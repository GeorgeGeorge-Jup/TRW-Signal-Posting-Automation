import os
import json
import time
import requests
from datetime import datetime
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
# TRW authenticates via localStorage (not cookies).
# The rauth value is loaded once at import, purged from environment immediately,
# and kept in memory as a string for reuse across scheduled runs.

_RAUTH_RAW: str = os.environ.get("TRW_RAUTH", "")
os.environ.pop("TRW_RAUTH", None)  # purge immediately — never appears in logs again

def _get_rauth() -> str:
    if not _RAUTH_RAW:
        raise EnvironmentError(
            "TRW_RAUTH environment variable is not set. "
            "Set it in Railway -> Service -> Variables."
        )
    # Validate it parses as JSON (it should be a JSON object string)
    try:
        json.loads(_RAUTH_RAW)
    except json.JSONDecodeError:
        raise ValueError(
            "TRW_RAUTH is not valid JSON. "
            "Copy the exact value of the rauth key from localStorage."
        )
    return _RAUTH_RAW


# ── Hyperliquid ───────────────────────────────────────────────────────────────
def fetch_positions():
    # Retry up to 5 times with exponential backoff for rate limit responses
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

    # Weight = position notional as % of total account equity (NAV).
    # Cash = whatever equity is not deployed into positions.
    for p in raw:
        p["weight"] = (p["notional"] / account_val * 100) if account_val else 0

    cash_pct = max(0.0, 100.0 - sum(p["weight"] for p in raw))

    return raw, cash_pct


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

        # Navigate to the domain first so we can write to localStorage
        print("Injecting auth into localStorage...")
        page.goto("https://app.jointherealworld.com", wait_until="commit", timeout=30_000)
        page.evaluate(f"localStorage.setItem('rauth', {json.dumps(rauth)})")

        # Now navigate to the channel — app will read localStorage and authenticate
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
        input_el.click()

        # Type line by line, using Shift+Enter for line breaks.
        # Plain Enter would submit the message mid-way through.
        lines = message.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.type(line, delay=10)
            if i < len(lines) - 1:
                page.keyboard.press("Shift+Enter")

        # Final Enter submits the complete message
        page.keyboard.press("Enter")

        page.wait_for_timeout(3_000)
        browser.close()


# ── Job (called by runner) ────────────────────────────────────────────────────
def run_job():
    print(f"\n{'='*50}")
    print(f"Job started -- {datetime.utcnow().isoformat()}")

    try:
        rauth = _get_rauth()
        print("Auth loaded securely.")

        positions, cash_pct = fetch_positions()
        print(f"Found {len(positions)} open position(s). Cash: {cash_pct:.1f}%")

        message = format_message(positions, cash_pct)
        print("\n-- Message preview --")
        print(message)
        print("\n-- Posting to TRW --")

        post_to_trw(message, rauth)
        print(f"Done -- {datetime.utcnow().isoformat()}")

    except Exception as e:
        print(f"ERROR: {e}")
        # Do not re-raise — let runner.py continue to the sleep loop
