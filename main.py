import os
import json
import math
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Config ────────────────────────────────────────────────────────────────────
VAULT_ADDRESS = "0xce508465b243216fcf372d3146fd62e8f7a7b8e2"
CHANNEL_URL   = "https://app.jointherealworld.com/chat/01GGDHGV32QWPG7FJ3N39K4FME/01H83QAX979K9R7QTMH74ATR8C"
INPUT_ID      = "01H83QAX979K9R7QTMH74ATR8C-input"
HL_API        = "https://api.hyperliquid.xyz/info"

# Where failed posts leave a screenshot and the open dialog's contents, so a
# miss can be diagnosed after the fact. Container disk is wiped on restart or
# redeploy; point DEBUG_DIR at a Railway volume to keep captures across those.
DEBUG_DIR             = os.environ.get("DEBUG_DIR", "/tmp/trw-debug")
DEBUG_RETENTION_HOURS = 48

# ── RSPS universe ─────────────────────────────────────────────────────────────
# Hyperliquid perps the RSPS can allocate to, keyed by the name shown in the
# post, with the emoji shown next to each. The xyz: builder-dex perps (stocks,
# commodities, indices) are deliberately not part of the RSPS.
COIN_EMOJI = {
    "BTC":      "🟠",
    "ETH":      "🔷",
    "SOL":      "🟣",
    "AVAX":     "🔺",
    "BNB":      "🟨",
    "LTC":      "🥈",
    "DOGE":     "🐶",
    "SUI":      "💧",
    "PEPE":     "🐸",
    "CRV":      "🌈",
    "LINK":     "🔗",
    "XRP":      "💀",
    "APT":      "🅰️",
    "AAVE":     "👻",
    "WLD":      "🌍",
    "TRX":      "🔴",
    "SHIB":     "🐕",
    "UNI":      "🦄",
    "DOT":      "🩷",
    "ADA":      "🔵",
    "PENDLE":   "⏳",
    "NEAR":     "🌐",
    "ONDO":     "🏦",
    "TAO":      "🧠",
    "ENA":      "💠",
    "HYPE":     "🟢",
    "FARTCOIN": "💨",
    "PAXG":     "🟡",
    "PUMP":     "💊",
    "XPL":      "🔌",
    "WLFI":     "🦅",
    "ASTER":    "✳️",
    "ZEC":      "🛡️",
    "MON":      "👾",
    "AERO":     "✈️",
    "LIT":      "🔥",
    "XMR":      "🕶️",
    "CASH":     "💵",
}

RSPS_COINS = frozenset(COIN_EMOJI) - {"CASH"}

# Case-insensitive lookup from an incoming coin name to the name shown in the post.
# Hyperliquid prefixes its 1000x-denominated perps with "k" (kPEPE = 1000 PEPE);
# that prefix only confuses readers, so those post as plain PEPE and SHIB.
_CANONICAL_COIN = {c.upper(): c for c in RSPS_COINS}
_CANONICAL_COIN.update({"KPEPE": "PEPE", "KSHIB": "SHIB"})


def canonical_coin(coin: str) -> str:
    return _CANONICAL_COIN.get(coin.upper(), coin)


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
_pushed_signals = None   # parse_pushed_payload() output, e.g. {"allocation": {"ETH": 33.3, "USD": 66.7}, ...}
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

    def _authorized(self, query=None):
        # The debug pages accept the key as ?key= too, so they open in a browser.
        api_key = os.environ.get("SIGNAL_API_KEY", "")
        provided = self.headers.get("x-api-key", "")
        if not provided and query:
            provided = query.get("key", [""])[0]
        return bool(api_key) and provided == api_key

    def _serve_debug(self, path, query):
        if not self._authorized(query):
            self._send_json(401, {"error": "unauthorized"})
            return
        files = list_debug_captures()
        if path in ("/debug", "/debug/"):
            self._send_json(200, {"retention_hours": DEBUG_RETENTION_HOURS, "files": files})
            return
        # Only names that are actually in the capture folder are served, so a
        # crafted path can't reach anything else on disk.
        name = path[len("/debug/"):]
        if name not in files:
            self._send_json(404, {"error": "not found"})
            return
        with open(os.path.join(DEBUG_DIR, name), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if name.endswith(".png") else "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/debug" or url.path.startswith("/debug/"):
            self._serve_debug(url.path, parse_qs(url.query))
        elif self.path == "/health":
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

        try:
            signal = parse_pushed_payload(payload)
        except ValueError as e:
            print(f"Rejected signal push — {e}")
            self._send_json(400, {"error": str(e)})
            return

        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        _set_pushed_signals(signal, timestamp)
        print(f"Signals received from TV-dHEDGE bot: {signal}")
        self._send_json(200, {"status": "ok", "received": signal["allocation"]})


def _pct_map(payload, key, required=False):
    """
    Read one {coin: pct} block of a push, or None when an optional block is
    absent. Every value must be a finite number: a bad value is refused so the
    job falls back to Hyperliquid instead of posting a garbled signal.
    """
    block = payload.get(key)
    if block is None and not required:
        return None
    if not isinstance(block, dict) or (required and not block):
        raise ValueError(f"missing or invalid '{key}' field")
    for coin, pct in block.items():
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct):
            raise ValueError(f"'{key}.{coin}' is not a number: {pct!r}")
    return dict(block)


def parse_pushed_payload(payload):
    """
    Validate a push from the TV-dHEDGE bot and keep the parts the post needs.

    "allocation" is the net book actually traded (negative = short) and is the
    only required field, so the original long-only pushes still work. Pushes
    that carry the short module (BRAS) add the two sleeves separately:
      "longs":  the RSPS long book, percent, including USD (cash)
      "shorts": the short module's targets, percent, negative = short
      "bras":   short module metadata, e.g. {"mode": "live", "gross_pct": 7.12}
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    signal = {
        "allocation": _pct_map(payload, "allocation", required=True),
        "longs":      _pct_map(payload, "longs"),
        "shorts":     _pct_map(payload, "shorts"),
        "bras":       payload.get("bras"),
    }

    if signal["longs"] and any(pct < 0 for pct in signal["longs"].values()):
        raise ValueError("'longs' must not contain negative weights")
    if signal["shorts"] and any(pct > 0 for pct in signal["shorts"].values()):
        raise ValueError("'shorts' must not contain positive weights")
    if signal["bras"] is not None and not isinstance(signal["bras"], dict):
        raise ValueError("'bras' must be an object")

    return signal


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

    # Shorts are a separate sleeve, not funded from cash, so only longs use it up.
    cash_pct = max(0.0, 100.0 - sum(p["weight"] for p in raw if p["direction"] == "LONG"))

    return raw, cash_pct


# Below this a weight shows as 0.0% in the post, so a coin netted to it is flat.
FLAT_PCT = 0.05


def _by_coin(weights: dict) -> dict:
    """Sum {coin: pct} by the name shown in the post (kPEPE and PEPE are one coin)."""
    out = {}
    for coin, pct in weights.items():
        if coin == "USD":
            continue
        name = canonical_coin(coin)
        out[name] = out.get(name, 0.0) + pct
    return out


def pushed_signals_to_positions(signal: dict):
    """
    Convert a parse_pushed_payload() signal into (positions, cash_pct,
    short_module) for format_message().

    positions/cash_pct are the RSPS long book, in the same format that
    fetch_positions() returns. short_module is None for a long-only push, so
    those post exactly as before; otherwise it holds the short sleeve and the
    net book (longs and shorts on the same coin netted into one position).
    """
    allocation = signal["allocation"]
    longs = signal["longs"]
    shorts = signal["shorts"]
    bras = signal["bras"]

    # A push without the separate sleeves is a net book: its negatives are shorts.
    if longs is None:
        longs = {coin: pct for coin, pct in allocation.items() if pct > 0}
    if shorts is None:
        shorts = {coin: pct for coin, pct in allocation.items() if coin != "USD" and pct < 0}

    cash_pct = longs.get("USD", allocation.get("USD", 0))
    positions = [
        {"coin": coin, "direction": "LONG", "notional": pct, "weight": pct}
        for coin, pct in _by_coin(longs).items()
        if pct > 0
    ]
    positions.sort(key=lambda x: x["weight"], reverse=True)

    has_short_module = bras is not None or signal["shorts"] is not None or bool(shorts)
    if not has_short_module:
        return positions, cash_pct, None

    if bras and bras.get("mode", "live") != "live":
        # A dry-run short book is not a signal anyone should follow.
        print(f"Short module is in {bras.get('mode')!r} mode — not posting its shorts")
        return positions, cash_pct, None

    short_by_coin = {coin: -pct for coin, pct in _by_coin(shorts).items() if pct < 0}
    long_by_coin = {p["coin"]: p["weight"] for p in positions}
    net_alloc = _by_coin(allocation)

    short_positions = [
        {"coin": coin, "direction": "SHORT", "notional": pct, "weight": pct}
        for coin, pct in short_by_coin.items()
    ]
    short_positions.sort(key=lambda x: x["weight"], reverse=True)

    net = []
    for coin in {**long_by_coin, **short_by_coin}:
        long_pct = long_by_coin.get(coin, 0.0)
        short_pct = short_by_coin.get(coin, 0.0)
        net_pct = long_pct - short_pct
        # The pushed allocation is what is actually traded, so it wins; the
        # sleeves should always add up to it.
        if coin in net_alloc:
            if abs(net_alloc[coin] - net_pct) > FLAT_PCT:
                print(
                    f"WARNING: {coin} net allocation {net_alloc[coin]:.2f}% does not match "
                    f"longs {long_pct:.2f}% minus shorts {short_pct:.2f}%; posting the allocation"
                )
            net_pct = net_alloc[coin]
        if abs(net_pct) < FLAT_PCT:
            direction = "FLAT"
        else:
            direction = "LONG" if net_pct > 0 else "SHORT"
        net.append({
            "coin":      coin,
            "direction": direction,
            "weight":    abs(net_pct),
            "long":      long_pct,
            "short":     short_pct,
            "netted":    long_pct > 0 and short_pct > 0,
        })
    order = {"LONG": 0, "SHORT": 1, "FLAT": 2}
    net.sort(key=lambda x: (order[x["direction"]], -x["weight"]))

    gross = sum(short_by_coin.values())
    if bras and isinstance(bras.get("gross_pct"), (int, float)) and abs(bras["gross_pct"] - gross) > FLAT_PCT:
        print(f"WARNING: short module gross_pct {bras['gross_pct']}% does not match its shorts ({gross:.2f}%)")

    short_module = {"shorts": short_positions, "gross": gross, "net": net}
    return positions, cash_pct, short_module


# ── Message formatter ─────────────────────────────────────────────────────────
def _coin_and_emoji(coin):
    coin = canonical_coin(coin)
    if coin not in RSPS_COINS:
        # Still posted, so the weights add up — just flagged in the logs.
        print(f"WARNING: {coin} is not in the RSPS token list")
    return coin, COIN_EMOJI.get(coin, "⚪")


def _position_line(p):
    coin, emoji = _coin_and_emoji(p["coin"])
    return f"- **{p['weight']:.1f}% {coin} {p['direction']}** {emoji}"


# Shown after the coin's emoji in the net book, so the bet's direction reads at a glance.
DIRECTION_ARROW = {"LONG": "⬆️", "SHORT": "⬇️", "FLAT": "➖"}


def _net_line(p):
    coin, emoji = _coin_and_emoji(p["coin"])
    arrow = DIRECTION_ARROW[p["direction"]]
    if p["direction"] == "FLAT":
        line = f"- **0.0% {coin} FLAT** {emoji} {arrow}"
    else:
        line = f"- **{p['weight']:.1f}% {coin} {p['direction']}** {emoji} {arrow}"
    if p["netted"]:
        line += f" (net of {p['long']:.1f}% long and {p['short']:.1f}% short)"
    return line


def format_message(positions, cash_pct, short_module=None):
    """
    Build the post. positions/cash_pct are the RSPS long book. short_module
    (see pushed_signals_to_positions) adds the short sleeve and the netted
    book; without it the post is the original long-only layout.
    """
    cash_line = f"- **{cash_pct:.1f}% CASH** {COIN_EMOJI['CASH']}"

    position_lines = [_position_line(p) for p in positions]
    position_lines.append(cash_line)
    sections = ["📈 **RSPS Signal:** 📈\n" + "\n".join(position_lines)]

    if short_module is not None:
        if short_module["shorts"]:
            short_lines = [_position_line(p) for p in short_module["shorts"]]
            short_lines.append(f"- Total short exposure: **{short_module['gross']:.1f}%**")
        else:
            short_lines = ["- **No short positions** (flat)"]
        sections.append("📉 **Short Module:** 📉\n" + "\n".join(short_lines))

        if short_module["shorts"]:
            net_lines = [_net_line(p) for p in short_module["net"]]
            net_lines.append(cash_line)
            sections.append("⚖️ **Net Positions:** ⚖️\n" + "\n".join(net_lines))

    positions_block = "\n\n".join(sections)

    return (
        f"⚡ **Portfolio Signal Update** ⚡\n\n"
        f"{DIVIDER}\n\n"
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


# ── Failure captures ──────────────────────────────────────────────────────────
def list_debug_captures():
    """Capture files still within the retention window, newest first."""
    if not os.path.isdir(DEBUG_DIR):
        return []
    cutoff = time.time() - DEBUG_RETENTION_HOURS * 3600
    files = []
    for name in os.listdir(DEBUG_DIR):
        path = os.path.join(DEBUG_DIR, name)
        if os.path.isfile(path) and os.path.getmtime(path) >= cutoff:
            files.append(name)
    return sorted(files, reverse=True)


def _prune_debug_captures():
    cutoff = time.time() - DEBUG_RETENTION_HOURS * 3600
    for name in os.listdir(DEBUG_DIR):
        path = os.path.join(DEBUG_DIR, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)


def capture_failure(page):
    """
    Save a screenshot of the page and whatever is open in TRW's dialog layer,
    and print the dialog text, so a failed post can be diagnosed afterwards.
    Never raises: a failed capture must not hide the error that triggered it.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        _prune_debug_captures()
        stem = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        shot = f"{stem}_failure.png"
        page.screenshot(path=os.path.join(DEBUG_DIR, shot), full_page=True, timeout=15_000)

        dialog = page.evaluate("""() => {
            const root = document.getElementById('dialog-root');
            return root ? {text: root.innerText, html: root.innerHTML} : null;
        }""")
        info = f"{stem}_failure.txt"
        with open(os.path.join(DEBUG_DIR, info), "w", encoding="utf-8") as f:
            f.write(f"URL: {page.url}\n\n")
            if dialog is None:
                f.write("No #dialog-root on the page.\n")
            else:
                f.write(f"Dialog text:\n{dialog['text']}\n\nDialog HTML:\n{dialog['html']}\n")

        print(f"Failure capture saved: {shot}, {info} (kept {DEBUG_RETENTION_HOURS}h, "
              "view via /debug?key=<SIGNAL_API_KEY>)")
        if dialog and dialog["text"].strip():
            print(f"Open dialog text: {dialog['text'].strip()[:1000]}")
    except Exception as e:
        print(f"Failure capture could not be saved: {e}")


class TransientPostError(Exception):
    """
    A failure that happened before the message was submitted, and that a later
    attempt has a realistic chance of getting past (page load timeouts, the
    input element not appearing in time). Safe to retry: nothing was sent.
    """


class PermanentPostError(Exception):
    """
    A failure that retrying cannot fix, or that must not be retried because we
    can no longer be sure whether the message went out. Expired auth is the
    former; a post-send verification failure is the latter — re-typing the
    message risks a duplicate post in the channel, which is worse than a miss.
    """


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
        try:
            _post_in_page(page, message, rauth)
        except Exception:
            capture_failure(page)
            raise
        finally:
            browser.close()


def _post_in_page(page, message, rauth):
    print("Injecting auth into localStorage...")
    try:
        page.goto("https://app.jointherealworld.com", wait_until="commit", timeout=30_000)
    except PlaywrightTimeoutError as e:
        raise TransientPostError(f"Initial page load timed out: {e}")
    page.evaluate("(token) => localStorage.setItem('rauth', token)", rauth)

    # TRW is a chat SPA with long-lived websockets and background polling, so
    # the network never reliably goes idle — waiting for "networkidle" here
    # made navigation fail on a slow night even though the page was usable.
    # Commit to the navigation, then let the wait for the input element below
    # be the real readiness gate: that element existing is the only condition
    # we actually need before typing.
    print("Navigating to channel...")
    try:
        page.goto(CHANNEL_URL, wait_until="domcontentloaded", timeout=40_000)
    except PlaywrightTimeoutError as e:
        raise TransientPostError(f"Channel navigation timed out: {e}")

    if "/login" in page.url or "/auth" in page.url:
        raise PermanentPostError(
            "TRW redirected to login -- rauth token has expired. "
            "Copy a fresh rauth value from localStorage and update TRW_RAUTH in Railway."
        )

    selector = f'[id="{INPUT_ID}"]'
    print("Waiting for message input...")
    try:
        page.wait_for_selector(selector, timeout=30_000)
    except PlaywrightTimeoutError:
        # The channel page was reached but never rendered the composer. Most
        # likely still hydrating; a retry gets a fresh load.
        raise TransientPostError(
            f"Message input {INPUT_ID} did not appear within 30s — "
            "page may not have finished loading. Message was NOT sent."
        )

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
        raise TransientPostError(
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
        # Deliberately permanent: Enter was already pressed, so we cannot
        # tell a failed send from a slow one. Retrying risks posting the
        # signal twice in the channel, which is worse than missing it.
        raise PermanentPostError(
            "Message input still contains text after send — "
            "message likely NOT delivered. Not retrying, to avoid a double "
            "post. Refresh TRW_RAUTH in Railway and check the channel."
        )

    print("Message delivered successfully — input box cleared.")


POST_ATTEMPTS = 3
POST_BACKOFF  = (30, 60)   # seconds to wait before attempt 2, then attempt 3


def post_to_trw_with_retry(message, rauth):
    """
    Post the message, retrying only failures that happened before submission.

    A whole night's signal used to be lost to a single transient page load
    failure, since the scheduler simply slept until the next day. Each attempt
    launches a fresh browser, so a retry starts from a clean session rather
    than a half-loaded page.
    """
    for attempt in range(1, POST_ATTEMPTS + 1):
        try:
            post_to_trw(message, rauth)
            return
        except TransientPostError as e:
            if attempt == POST_ATTEMPTS:
                raise RuntimeError(
                    f"Posting failed after {POST_ATTEMPTS} attempts. Last error: {e}"
                )
            wait = POST_BACKOFF[attempt - 1]
            print(f"Attempt {attempt}/{POST_ATTEMPTS} failed: {e}")
            print(f"Retrying in {wait}s...")
            time.sleep(wait)


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
            positions, cash_pct, short_module = pushed_signals_to_positions(pushed)
        else:
            # Hyperliquid only holds the net book, so any short there is listed
            # with the longs; the sleeves can't be told apart.
            print("No pushed signals — falling back to Hyperliquid positions")
            positions, cash_pct = fetch_positions()
            short_module = None

        print(f"Found {len(positions)} open position(s). Cash: {cash_pct:.1f}%")
        if short_module is not None:
            print(f"Short module: {len(short_module['shorts'])} short(s), "
                  f"{short_module['gross']:.1f}% gross")

        message = format_message(positions, cash_pct, short_module)
        print("\n-- Message preview --")
        print(message)
        print("\n-- Posting to TRW --")

        post_to_trw_with_retry(message, rauth)
        _clear_pushed_signals()
        print(f"Done -- {datetime.utcnow().isoformat()}")

    except Exception as e:
        print(f"ERROR: {e}")
        # Do not re-raise — let the scheduler continue


# ── Scheduler ─────────────────────────────────────────────────────────────────
def seconds_until_next_run(hour=0, minute=12):
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
        wait = seconds_until_next_run(hour=0, minute=12)
        print(f"Next run in {wait/3600:.2f}h ({wait:.0f}s)")
        time.sleep(wait)
        run_job()
        time.sleep(60)  # prevent double-trigger at boundary
