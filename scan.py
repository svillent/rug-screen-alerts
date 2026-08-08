import os
import json
import time
import urllib.request
import urllib.error

# ---- config ----
MCAP_CEILING = 80000
LP_LOCK_THRESHOLD = 80  # percent
MAX_CHECKS_PER_RUN = 25
STATE_FILE = "seen_mints.json"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()



def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "rug-screen-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        print("Telegram send failed:", e.read())


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(seen):
    trimmed = list(seen)[-5000:]
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def fmt_usd(n):
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.2f}"


def main():
    seen = load_state()

    try:
        new_tokens = http_get("https://api.rugcheck.xyz/v1/stats/new_tokens")
    except Exception as e:
        print("Discovery feed failed:", e)
        return

    candidates = new_tokens if isinstance(new_tokens, list) else new_tokens.get("tokens", [])
    candidates = [c for c in candidates if c.get("mint") and c["mint"] not in seen][:MAX_CHECKS_PER_RUN]

    print(f"Screening {len(candidates)} new candidate(s)...")

    alerts_sent = 0
    for c in candidates:
        mint = c["mint"]
        seen.add(mint)
        try:
            report = http_get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
        except Exception as e:
            print(f"  {mint}: report fetch failed ({e})")
            continue

        try:
            dex = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            dex_pair = (dex.get("pairs") or [None])[0]
        except Exception:
            dex_pair = None

        market_cap = None
        if dex_pair:
            market_cap = dex_pair.get("marketCap") or dex_pair.get("fdv")

        lp_locked_pct = report.get("lpLockedPct")
        if lp_locked_pct is None:
            markets = report.get("markets") or []
            if markets and markets[0].get("lp"):
                lp_locked_pct = markets[0]["lp"].get("lpLockedPct", 0)
            else:
                lp_locked_pct = 0

        insider_networks = report.get("insiderNetworks") or (report.get("graphInsiderReport") or {}).get("networks") or []
        insider_count = len(insider_networks) if isinstance(insider_networks, list) else 0

        passes_liquidity = lp_locked_pct >= LP_LOCK_THRESHOLD
        passes_mcap = market_cap is not None and 0 < market_cap < MCAP_CEILING
        passes_insiders = insider_count == 0

        name = (report.get("tokenMeta") or {}).get("name") or (dex_pair or {}).get("baseToken", {}).get("name") or "Unknown"
        symbol = (report.get("tokenMeta") or {}).get("symbol") or (dex_pair or {}).get("baseToken", {}).get("symbol") or "???"

        if passes_liquidity and passes_mcap and passes_insiders:
            text = (
                f"🚨 <b>{name} (${symbol})</b> passed screening\n\n"
                f"Market cap: {fmt_usd(market_cap)}\n"
                f"LP locked: {lp_locked_pct:.0f}%\n"
                f"Insider clusters detected: {insider_count}\n\n"
                f"Mint: <code>{mint}</code>\n"
                f"Chart: https://dexscreener.com/solana/{mint}\n"
                f"Full report: https://rugcheck.xyz/tokens/{mint}\n\n"
                f"Heuristic screen only — not financial advice. DYOR."
            )
            send_telegram(text)
            alerts_sent += 1
            print(f"  {symbol}: ALERT SENT")
        else:
            print(f"  {symbol}: skip (mcap={market_cap}, lp={lp_locked_pct}, insiders={insider_count})")

        time.sleep(0.5)

    save_state(seen)
    print(f"Done. {alerts_sent} alert(s) sent this run.")


if __name__ == "__main__":
    main()
