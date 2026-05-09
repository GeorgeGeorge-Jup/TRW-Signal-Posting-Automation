import os
import json
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
VAULT_ADDRESS = "0xce508465b243216fcf372d3146fd62e8f7a7b8e2"
CHANNEL_URL   = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C"
INPUT_ID      = "01H83QAX979K9R7QTMH74ATR8C-input"
HL_API        = "https://api.hyperliquid.xyz/info"

# ── Coin emojis ───────────────────────────────────────────────────────────────
COIN_EMOJI = {
    "BTC":  "🟠",
    "ETH":  "🔷",
    "SOL":  "🟣",
    "SUI":  "💧",
    "XRP":  "💀",
    "BNB":  "🟨",
    "DOGE": "🐶",
    "HYPE": "🟢",
    "PAXG": "🟡",
    "CASH": "💵",
}

DIVIDER = "───── ⋆⋅☆⋅⋆ ─────"

# ── Auth safety ──────────────────────────────────────────────────────────────
_RAUTH_RAW: str = os.environ.get("TRW_RAUTH", "")
os.environ.pop("TRW_RAUTH", None)

def _get_rauth() -> str:
    if not _RAUTH_RAW:
        raise EnvironmentError(
            "TRW_RAUTH environment variable is not set. "
            "Set it in Railway -> Service -> Variables."
        )
    try:
        json.loads(_RAUTH_RAW)
    except json.JSONDecodeError:
        raise ValueError(
            "TRW_RAUTH is not valid JSON. "
            "Copy the exact value of the rauth key from localStorage."
        )
    return _RAUTH_RAW


# ── Pushed signal store ───────────────────────────────────────────────────────
# Written by the HTTP receiver thread, read by run_job().
# None = no push received yet (fall back to Hyperliquid).

_signal_lock = threading.Lock()
_pushed_signals = None   # {asset: pct, ...} e.g. {"ETH": 33.3, "SOL": 25.8, "USD": 40.9}
_push_timestamp = None

def _set_pushed_signals(signals, timestamp):
    global _pushed_signals, _push_timestamp
    with _signal_lock:
        _pushed_signals = signals
        _push_timestamp = timestamp

def _get_pushed_signals():
    with _signal_lock:
        return _pushed_signals, _push_timestamp

def _clear_pushed_signals():
    global _pushed_signals, _push_timestamp
    with _signal_lock:
        _pushed_signals = None
        _push_timestamp = None


# ── HTTP receiver ─────────────────────────────────────────────────────────────

class SignalReceiver(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress noisy default HTTP logs

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            signals, ts = _get_pushed_signals()
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

        api_key = os.environ.get("SIGNAL_API_KEY", "")
        provided = self.headers.get("x-api-key", "")
        if not api_key or provided != api_key:
            print(f"Rejected signal push — bad API key")
            self._send_json(401, {"error": "unauthorized"})
            return

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

        _set_pushed_signals(allocation, timestamp)
        print(f"Signals received from TV-dHEDGE bot: {allocation}")
        self._send_json(200, {"status": "ok", "received": allocation})


def start_receiver_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SignalReceiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Signal receiver listening on port {port}")


# ── Hyperliquid ───────────────────────────────────────────────────────────────
def fetch_positions():
    delays = [5, 15, 30, 60, 120]
    for attempt, delay in enumerate(delays, 1):
        resp = requests.post(
            HL_API,
            json={"type": "clearinghouseState", "user": VAULT_ADDRESS},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 429:
            if attempt < len(delays):
                print(f"Rate limited by Hyperliquid (attempt {attempt}). Retrying in {delay}s...")
                time.sleep(delay)
                continue
            else:
                resp.raise_for_status()
        resp.raise_for_status()
        break

    data = resp.json()

    margin      = data.get("marginSummary", {})
    account_val = float(margin.get("accountValue", 0))

    raw = []
    for item in data.get("assetPositions", []):
        pos  = item["position"]
        size = float(pos["szi"])
        if size == 0:
            continue
        raw.append({
            "coin":      pos["coin"],
            "direction": "LONG" if size > 0 else "SHORT",
            "notional":  abs(float(pos["positionValue"])),
        })

    raw.sort(key=lambda x: x["notional"], reverse=True)

    for p in raw:
        p["weight"] = (p["notional"] / account_val * 100) if account_val else 0

    cash_pct = max(0.0, 100.0 - sum(p["weight"] for p in raw))

    return raw, cash_pct


def pushed_signals_to_positions(signals: dict):
    """
    Convert a pushed allocation dict {"ETH": 33.3, "SOL": 25.8, "USD": 40.9}
    into the same (positions, cash_pct) format that fetch_positions() returns,
    so the rest of run_job() works unchanged.
    """
    cash_pct = signals.get("USD", 0)
    positions = []
    for coin, pct in signals.items():
        if coin == "USD" or pct <= 0:
            continue
        positions.append({
            "coin":      coin,
            "direction": "LONG",
            "notional":  pct,   # notional not needed for display, weight is what matters
            "weight":    pct,
        })
    positions.sort(key=lambda x: x["weight"], reverse=True)
    return positions, cash_pct


# ── Message formatter ─────────────────────────────────────────────────────────
def format_message(positions, cash_pct):
    position_lines = []
    for p in positions:
        emoji = COIN_EMOJI.get(p["coin"], "⚪")
        position_lines.append(f"- **{p['weight']:.1f}% {p['coin']} {p['direction']}** {emoji}")
    position_lines.append(f"- **{cash_pct:.1f}% CASH** {COIN_EMOJI['CASH']}")

    positions_block = "\n".join(position_lines)

    return (
        f"⚡ **Portfolio Signal Update** ⚡\n\n"
        f"{DIVIDER}\n\n"
        f"📈 **RSPS Signal:** 📈\n"
        f"{positions_block}\n\n"
        f"{DIVIDER}\n\n"
        f"Executive Summary: - Positions will continue to be actively managed. "
        f"Check in every day, signals can change frequently.\n\n"
        f"Associated Data: - BTC Leverage condition = BTC Leverage Impermissible ❌💀\n\n"
        f"<@role:01J9FWHFR0FM7M95MCH30DV525>  "
        f"[For practical information related to the signals, please refer to this attached post & attached lesson:]"
        f"(https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C/01JJV933W0GBNX0TXBCGFESJG9) "
        f"https://app.jointherealworld.com/lesson/BqkUG0Vh?server=01GGDHGV32QWPG7FJ3N39K4FME"
    )


# ── TRW poster ────────────────────────────────────────────────────────────────
def _get_input_text(input_el) -> str:
    """
    Read the current text content of the chat input element via the DOM.
    inner_text() is unreliable for contenteditable divs in headless mode —
    it can return "" even when the element has content, producing false positives.
    Reading textContent directly via JS is the reliable alternative.
    """
    return input_el.evaluate("el => el.textContent || el.value || ''")


def post_to_trw(message, rauth):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.on("console", lambda _: None)

        print("Injecting auth into localStorage...")
        page.goto("https://app.jointherealworld.com", wait_until="commit", timeout=30_000)
        page.evaluate("(token) => localStorage.setItem('rauth', token)", rauth)

        print("Navigating to channel...")
        page.goto(CHANNEL_URL, wait_until="networkidle", timeout=40_000)

        if "/login" in page.url or "/auth" in page.url:
            browser.close()
            raise RuntimeError(
                "TRW redirected to login -- rauth token has expired. "
                "Copy a fresh rauth value from localStorage and update TRW_RAUTH in Railway."
            )

        selector = f'[id="{INPUT_ID}"]'
        print("Waiting for message input...")
        page.wait_for_selector(selector, timeout=20_000)

        input_el = page.locator(selector)

        # click() alone is not always sufficient to capture keyboard focus in
        # headless mode, especially when a background thread (the HTTP server)
        # is running. Calling focus() explicitly after click() guarantees that
        # subsequent page.keyboard.type() calls land in the right element.
        input_el.click()
        input_el.focus()

        lines = message.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.type(line, delay=10)
            if i < len(lines) - 1:
                page.keyboard.press("Shift+Enter")

        # ── Pre-send sanity check ─────────────────────────────────────────────
        # Verify that content was actually typed into the input before we send.
        # If this is empty it means focus was never captured and the typing went
        # nowhere — catching it here surfaces a clear error instead of silently
        # "succeeding" with a blank submit.
        pre_send = _get_input_text(input_el)
        if not pre_send.strip():
            browser.close()
            raise RuntimeError(
                "Input box is empty before sending — keyboard focus was never captured. "
                "The message was NOT sent. Check that INPUT_ID is still correct and "
                "that the page loaded fully."
            )
        print(f"Pre-send check passed — {len(pre_send)} chars staged in input.")

        # Submit
        page.keyboard.press("Enter")

        # ── Post-send verification ────────────────────────────────────────────
        # Wait for the chat framework to clear the input, then confirm via the
        # DOM (not inner_text(), which is unreliable for contenteditable divs).
        page.wait_for_timeout(5_000)
        remaining = _get_input_text(input_el)
        if remaining.strip():
            browser.close()
            raise RuntimeError(
                "Message input still contains text after send — "
                "message likely NOT delivered. Refresh TRW_RAUTH in Railway."
            )

        print("Message delivered successfully — input box cleared.")
        browser.close()


# ── Job ────────────────────────────────────────────────────────────────────────
def run_job():
    print(f"\n{'='*50}")
    print(f"Job started -- {datetime.utcnow().isoformat()}")

    try:
        rauth = _get_rauth()
        print("Auth loaded securely.")

        # Use pushed signals from TV-dHEDGE bot if available, else fall back to Hyperliquid
        pushed, push_ts = _get_pushed_signals()
        if pushed is not None:
            print(f"Using pushed signals from TV-dHEDGE bot (timestamp: {push_ts})")
            positions, cash_pct = pushed_signals_to_positions(pushed)
        else:
            print("No pushed signals — falling back to Hyperliquid positions")
            positions, cash_pct = fetch_positions()

        print(f"Found {len(positions)} open position(s). Cash: {cash_pct:.1f}%")

        message = format_message(positions, cash_pct)
        print("\n-- Message preview --")
        print(message)
        print("\n-- Posting to TRW --")

        post_to_trw(message, rauth)
        _clear_pushed_signals()
        print(f"Done -- {datetime.utcnow().isoformat()}")

    except Exception as e:
        print(f"ERROR: {e}")
        # Do not re-raise — let the scheduler continue


# ── Scheduler ─────────────────────────────────────────────────────────────────
def seconds_until_next_run(hour=0, minute=10):
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


if __name__ == "__main__":
    start_receiver_server()

    print("Running job immediately on startup...")
    run_job()

    while True:
        wait = seconds_until_next_run(hour=0, minute=10)
        print(f"Next run in {wait/3600:.2f}h ({wait:.0f}s)")
        time.sleep(wait)
        run_job()
        time.sleep(60)  # prevent double-trigger at boundary
