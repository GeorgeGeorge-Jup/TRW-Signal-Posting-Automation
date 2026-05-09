"""
main.py — TRW Signal Poster

Posts daily portfolio signals to a TRW chat channel.

SIGNAL SOURCE (priority order):
  1. Pushed allocation from TV-dHEDGE bot via HTTP POST to /receive-signals
     This is faster and more accurate — signals are the parsed strategy output
     BEFORE trades execute, so they never lag behind slow order fills.
  2. Fallback: live Hyperliquid positions (original method) — used if the
     TV-dHEDGE bot failed to push or the container restarted since last push.

The receiver endpoint runs as a background thread on port 8080. Railway
exposes this publicly so the TV-dHEDGE bot can reach it.

TIMING:
  TV-dHEDGE bot runs at 00:01 UTC → signals parsed by ~00:03 UTC → pushes here.
  This bot posts to TRW at 00:10 UTC — 7+ minutes after signals arrive.
  No race condition.

Environment variables required:
  TRW_RAUTH              — TRW localStorage auth token
  SIGNAL_API_KEY         — shared secret, must match TV-dHEDGE bot's SIGNAL_API_KEY
  PORT                   — set automatically by Railway (default 8080)

Optional (only needed if Hyperliquid fallback is used):
  VAULT_ADDRESS          — Hyperliquid vault address for fallback position read
"""

import os
import time
import json
import threading
import logging
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from playwright.sync_api import sync_playwright

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TRW_CHANNEL_URL = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01GHJ1FA8N3DT7CFKXCB191WEY"
INPUT_ID = "01GHJ1FA8N3DT7CFKXCB191WEY-input"
VAULT_ADDRESS = os.environ.get("VAULT_ADDRESS", "0xce508465b243216fcf372d3146fd62e8f7a7b8e2")
HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

COIN_EMOJIS = {
    "BTC": "🟠", "ETH": "🔷", "SOL": "🟣", "SUI": "💧",
    "XRP": "💀", "BNB": "🟨", "DOGE": "🐶", "HYPE": "🟢",
    "PAXG": "🟡", "CASH": "💵",
}

# ── Shared signal store ───────────────────────────────────────────────────────
# Written by the receiver thread, read by the poster thread.
# None = no push received yet this cycle (use fallback).
_signal_lock = threading.Lock()
_pushed_signals: dict | None = None   # {asset: pct, ...} e.g. {"ETH": 33.3, "SOL": 25.8, "USD": 40.9}
_push_timestamp: str | None = None    # ISO string, for logging


def set_pushed_signals(signals: dict, timestamp: str):
    global _pushed_signals, _push_timestamp
    with _signal_lock:
        _pushed_signals = signals
        _push_timestamp = timestamp
    logger.info(f"Signal store updated: {signals}")


def get_pushed_signals() -> tuple[dict | None, str | None]:
    with _signal_lock:
        return _pushed_signals, _push_timestamp


def clear_pushed_signals():
    global _pushed_signals, _push_timestamp
    with _signal_lock:
        _pushed_signals = None
        _push_timestamp = None


# ── HTTP receiver ─────────────────────────────────────────────────────────────

class SignalReceiver(BaseHTTPRequestHandler):
    """
    Receives pushed signals from the TV-dHEDGE bot.

    POST /receive-signals
    Headers: x-api-key: <SIGNAL_API_KEY>
    Body: {"allocation": {"ETH": 33.33, "SOL": 25.83, "USD": 40.84}, "timestamp": "..."}

    GET /health
    Returns 200 OK with signal status.
    """

    def log_message(self, format, *args):
        # Suppress default HTTP server logging (noisy in Railway logs)
        pass

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            signals, ts = get_pushed_signals()
            self._send_json(200, {
                "status": "ok",
                "signal_received": signals is not None,
                "push_timestamp": ts,
                "signals": signals,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/receive-signals":
            self._send_json(404, {"error": "not found"})
            return

        # Auth check
        api_key = os.environ.get("SIGNAL_API_KEY", "")
        provided = self.headers.get("x-api-key", "")
        if not api_key or provided != api_key:
            logger.warning(f"Rejected signal push — bad API key (provided: {provided[:8]}...)")
            self._send_json(401, {"error": "unauthorized"})
            return

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"bad JSON: {e}"})
            return

        allocation = payload.get("allocation")
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())

        if not allocation or not isinstance(allocation, dict):
            self._send_json(400, {"error": "missing or invalid 'allocation' field"})
            return

        set_pushed_signals(allocation, timestamp)
        logger.info(f"✓ Signals received from TV-dHEDGE bot at {timestamp}")
        self._send_json(200, {"status": "ok", "received": allocation})


def start_receiver_server():
    """Start the HTTP receiver in a background daemon thread."""
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SignalReceiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Signal receiver listening on port {port}")
    return server


# ── Hyperliquid fallback ───────────────────────────────────────────────────────

def fetch_positions_from_hyperliquid() -> dict:
    """
    Fallback: read live positions from Hyperliquid.
    Returns {asset: pct} dict including CASH.
    Uses exponential backoff on 429s.
    """
    logger.info("Using Hyperliquid fallback for position data...")

    payload = {"type": "clearinghouseState", "user": VAULT_ADDRESS}
    delays = [5, 15, 30, 60, 120]

    for attempt, delay in enumerate(delays + [None]):
        try:
            resp = httpx.post(HYPERLIQUID_API, json=payload, timeout=15)
            if resp.status_code == 429:
                if delay is None:
                    raise RuntimeError("Hyperliquid 429 after all retries")
                logger.warning(f"Hyperliquid 429, retrying in {delay}s...")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.HTTPError as e:
            if delay is None:
                raise
            logger.warning(f"Hyperliquid error ({e}), retrying in {delay}s...")
            time.sleep(delay)

    perp = data.get("assetPositions", [])
    margin = data.get("crossMarginSummary", {})
    account_value = float(margin.get("accountValue", 0))

    positions = {}
    total_notional = 0.0

    for item in perp:
        pos = item.get("position", {})
        coin = pos.get("coin", "")
        size = abs(float(pos.get("szi", 0)))
        if size == 0:
            continue
        entry_px = float(pos.get("entryPx") or 0)
        notional = size * entry_px
        if notional > 0:
            positions[coin] = notional
            total_notional += notional

    if account_value <= 0:
        raise RuntimeError(f"Hyperliquid account value is {account_value}")

    result = {}
    for coin, notional in positions.items():
        result[coin] = round(notional / account_value * 100, 1)

    cash_pct = round(max(0, 100 - sum(result.values())), 1)
    result["USD"] = cash_pct

    logger.info(f"Hyperliquid fallback result: {result}")
    return result


# ── Signal resolution ─────────────────────────────────────────────────────────

def resolve_signals() -> tuple[dict, str]:
    """
    Return (allocation_dict, source_label).
    Tries pushed signals first, falls back to Hyperliquid.
    """
    signals, push_ts = get_pushed_signals()

    if signals is not None:
        logger.info(f"Using pushed signals from TV-dHEDGE bot (pushed at {push_ts})")
        return signals, f"strategy signals (pushed {push_ts})"

    logger.warning("No pushed signals — falling back to Hyperliquid positions")
    hl_signals = fetch_positions_from_hyperliquid()
    return hl_signals, "Hyperliquid positions (fallback)"


# ── Message formatting ────────────────────────────────────────────────────────

def format_message(allocation: dict, source: str) -> str:
    """
    Build the TRW signal post from allocation dict.
    Allocation keys: asset names (BTC, ETH, SOL, USD etc.)
    USD key = cash.
    """
    lines = []
    lines.append("───── ⋆⋅☆⋅⋆ ─────")
    lines.append("")
    lines.append("**📊 RSPS Daily Signal Update**")
    lines.append("")
    lines.append("**Current Portfolio Allocation:**")

    # Sort by allocation size descending, cash last
    sorted_assets = sorted(
        [(k, v) for k, v in allocation.items() if k != "USD"],
        key=lambda x: -x[1]
    )
    cash_pct = allocation.get("USD", 0)

    for asset, pct in sorted_assets:
        if pct <= 0:
            continue
        emoji = COIN_EMOJIS.get(asset, "⚪")
        lines.append(f"**{emoji} {asset}: {pct:.1f}%**")

    # Always show cash
    cash_emoji = COIN_EMOJIS["CASH"]
    lines.append(f"**{cash_emoji} CASH: {cash_pct:.1f}%**")

    lines.append("")
    lines.append("───── ⋆⋅☆⋅⋆ ─────")
    lines.append("")
    lines.append(
        "These signals are generated by the RSPS system — a multi-strategy "
        "momentum and trend-following framework running across 6 independent "
        "TradingView strategies, aggregated daily."
    )
    lines.append("")
    lines.append("<@role:01J9FWHFR0FM7M95MCH30DV525>")
    lines.append("")
    lines.append("📘 Learn more: https://www.skool.com/theinvestorsclub-4406/about")
    lines.append("💬 Questions? Drop them below.")

    return "\n".join(lines)


# ── TRW posting ───────────────────────────────────────────────────────────────

def post_to_trw(message: str):
    """Post message to TRW channel using Playwright + localStorage auth."""
    rauth = os.environ.get("TRW_RAUTH")
    if not rauth:
        raise ValueError("TRW_RAUTH environment variable not set")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()

        # Navigate to TRW first (need a page load before setting localStorage)
        page.goto("https://app.jointherealworld.com", wait_until="domcontentloaded", timeout=30000)
        page.evaluate("(token) => window.localStorage.setItem('rauth', token)", rauth)

        # Now navigate to the channel
        page.goto(TRW_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # Find input and type message with Shift+Enter for newlines
        input_el = page.locator(f"[id='{INPUT_ID}']")
        input_el.wait_for(timeout=15000)
        input_el.click()

        lines = message.split("\n")
        for i, line in enumerate(lines):
            if line:
                input_el.type(line)
            if i < len(lines) - 1:
                page.keyboard.press("Shift+Enter")

        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)

        browser.close()

    logger.info("Message posted to TRW successfully")


# ── Daily job ─────────────────────────────────────────────────────────────────

def run_job():
    """Main daily job: resolve signals, format, post to TRW."""
    logger.info(f"=== TRW Signal Post starting — {datetime.now(timezone.utc).isoformat()} ===")

    try:
        allocation, source = resolve_signals()
        logger.info(f"Signal source: {source}")
        logger.info(f"Allocation: {allocation}")

        message = format_message(allocation, source)
        logger.info("Formatted message:\n" + message)

        post_to_trw(message)
        logger.info("=== Job complete ===")

        # Clear after successful post so stale signals don't carry over
        clear_pushed_signals()

    except Exception as e:
        logger.error(f"Job failed: {e}")
        raise


# ── Scheduler entry point ─────────────────────────────────────────────────────

def seconds_until_next_run(hour=0, minute=10):
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main():
    logger.info("TRW Signal Poster starting up...")

    # Start the HTTP receiver (background thread)
    start_receiver_server()

    # Run immediately on startup for testing
    logger.info("Running job immediately on startup...")
    try:
        run_job()
    except Exception as e:
        logger.error(f"Startup run failed: {e}")

    # Then sleep until 00:10 UTC each day
    while True:
        wait = seconds_until_next_run(hour=0, minute=10)
        logger.info(f"Next scheduled run in {wait/3600:.2f}h ({wait:.0f}s)")
        time.sleep(wait)
        try:
            run_job()
        except Exception as e:
            logger.error(f"Scheduled run failed: {e}")
        # Brief sleep to avoid double-triggering at exactly midnight
        time.sleep(60)


if __name__ == "__main__":
    main()
