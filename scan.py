import os
import json
import time
import urllib.request
import urllib.error
import urllib.parse

# ---- config ----
MCAP_CEILING = 80000
LP_LOCK_THRESHOLD = 80  # percent
MAX_AGE_MINUTES = 60
MAX_CHECKS_PER_RUN = 25
CLUSTER_WARNING_THRESHOLD = 5  # percent — flag if any single holder exceeds this
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
        print("Telegram send failed (HTTP error):", e.read())
    except Exception as e:
        print("Telegram send failed (network/other error):", e)


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


def gather_candidates(seen):
    """Pull candidate mints from two independent discovery sources and merge them."""
    candidates_by_mint = {}

    # Source 1: RugCheck's new-token discovery feed
    try:
        new_tokens = http_get("https://api.rugcheck.xyz/v1/stats/new_tokens")
        if isinstance(new_tokens, list):
            raw = new_tokens
        elif isinstance(new_tokens, dict):
            raw = new_tokens.get("tokens", [])
        else:
            raw = []
        for item in raw:
            if isinstance(item, dict) and item.get("mint"):
                candidates_by_mint[item["mint"]] = item
    except Exception as e:
        print("RugCheck discovery feed failed:", e)

    # Source 2: Dexscreener's latest token profiles feed, Solana only
    try:
        profiles = http_get("https://api.dexscreener.com/token-profiles/latest/v1")
        if isinstance(profiles, list):
            for item in profiles:
                if (
                    isinstance(item, dict)
                    and item.get("chainId") == "solana"
                    and item.get("tokenAddress")
                ):
                    mint = item["tokenAddress"]
                    if mint not in candidates_by_mint:
                        candidates_by_mint[mint] = {"mint": mint}
    except Exception as e:
        print("Dexscreener discovery feed failed:", e)

    candidates = [
        c for mint, c in candidates_by_mint.items()
        if mint not in seen
    ][:MAX_CHECKS_PER_RUN]

    return candidates


def get_cluster_warning(report):
    """Check RugCheck's topHolders data for any single wallet above the warning threshold."""
    top_holders = report.get("topHolders")
    if not isinstance(top_holders, list) or not top_holders:
        return None  # no data available, not a claim either way

    max_pct = 0.0
    for holder in top_holders:
        if not isinstance(holder, dict):
            continue
        raw_pct = holder.get("pct")
        try:
            pct = float(raw_pct)
        except (TypeError, ValueError):
            continue
        if pct > max_pct:
            max_pct = pct

    return max_pct


def main():
    seen = load_state()
    candidates = gather_candidates(seen)

    print(f"Screening {len(candidates)} new candidate(s)...")

    alerts_sent = 0
    for c in candidates:
        mint = c.get("mint")
        if not mint:
            continue
        seen.add(mint)

        try:
            report = http_get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
            if not isinstance(report, dict):
                print(f"  {mint}: unexpected report format, skipping")
                continue

            try:
                dex = http_get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
                dex_pairs = dex.get("pairs") if isinstance(dex, dict) else None
                dex_pair = dex_pairs[0] if dex_pairs else None
                if not isinstance(dex_pair, dict):
                    dex_pair = None
            except Exception:
                dex_pair = None

            # age check — best effort. Missing data means we proceed without
            # this particular filter rather than reject the token outright.
            raw_age_value = report.get("detectedAt") or (dex_pair or {}).get("pairCreatedAt")
            age_ms_timestamp = None
            if raw_age_value is not None:
                try:
                    age_ms_timestamp = float(raw_age_value)
                except (TypeError, ValueError):
                    age_ms_timestamp = None

            if age_ms_timestamp:
                age_minutes = (time.time() * 1000 - age_ms_timestamp) / 60000
                if age_minutes > MAX_AGE_MINUTES:
                    print(f"  {mint}: skip (age {age_minutes:.1f}m > {MAX_AGE_MINUTES}m limit)")
                    continue
            else:
                print(f"  {mint}: no age data available, proceeding without age filter")

            # market cap
            market_cap = None
            if dex_pair:
                raw_mcap = dex_pair.get("marketCap") or dex_pair.get("fdv")
                if raw_mcap is not None:
                    try:
                        market_cap = float(raw_mcap)
                    except (TypeError, ValueError):
                        market_cap = None

            # LP locked %
            raw_lp = report.get("lpLockedPct")
            if raw_lp is None:
                markets = report.get("markets")
                if isinstance(markets, list) and markets and isinstance(markets[0], dict):
                    lp_info = markets[0].get("lp")
                    raw_lp = lp_info.get("lpLockedPct", 0) if isinstance(lp_info, dict) else 0
                else:
                    raw_lp = 0
            try:
                lp_locked_pct = float(raw_lp)
            except (TypeError, ValueError):
                lp_locked_pct = 0.0

            # insider clusters
            graph_report = report.get("graphInsiderReport")
            graph_report = graph_report if isinstance(graph_report, dict) else {}
            insider_networks = report.get("insiderNetworks") or graph_report.get("networks") or []
            insider_count = len(insider_networks) if isinstance(insider_networks, list) else 0

            # top holder cluster check (informational, does not gate the alert)
            max_holder_pct = get_cluster_warning(report)

            passes_liquidity = lp_locked_pct >= LP_LOCK_THRESHOLD
            passes_mcap = market_cap is not None and 0 < market_cap < MCAP_CEILING
            passes_insiders = insider_count == 0

            token_meta = report.get("tokenMeta")
            token_meta = token_meta if isinstance(token_meta, dict) else {}
            base_token = (dex_pair or {}).get("baseToken")
            base_token = base_token if isinstance(base_token, dict) else {}
            name = token_meta.get("name") or base_token.get("name") or "Unknown"
            symbol = token_meta.get("symbol") or base_token.get("symbol") or "???"

            if passes_liquidity and passes_mcap and passes_insiders:
                if max_holder_pct is None:
                    cluster_line = "Top holder data: unavailable"
                elif max_holder_pct > CLUSTER_WARNING_THRESHOLD:
                    cluster_line = f"⚠️ Largest single holder: {max_holder_pct:.1f}% (above {CLUSTER_WARNING_THRESHOLD}% threshold)"
                else:
                    cluster_line = f"✅ Largest single holder: {max_holder_pct:.1f}% (below {CLUSTER_WARNING_THRESHOLD}% threshold)"

                search_query = urllib.parse.quote(symbol if symbol != "???" else mint)
                twitter_link = f"https://x.com/search?q={search_query}&src=typed_query"
                telegram_buzz_link = f"https://www.google.com/search?q=site:t.me+{search_query}"
                bubblemaps_link = f"https://v2.bubblemaps.io/sol/token/{mint}"

                text = (
                    f"🚨 <b>{name} (${symbol})</b> passed screening\n\n"
                    f"Market cap: {fmt_usd(market_cap)}\n"
                    f"LP locked: {lp_locked_pct:.0f}%\n"
                    f"Insider clusters detected: {insider_count}\n"
                    f"{cluster_line}\n\n"
                    f"Mint: <code>{mint}</code>\n"
                    f"Chart: https://dexscreener.com/solana/{mint}\n"
                    f"Full report: https://rugcheck.xyz/tokens/{mint}\n"
                    f"Bubblemaps (unverified link format — please confirm it loads): {bubblemaps_link}\n"
                    f"X/Twitter search: {twitter_link}\n"
                    f"Telegram mentions (via Google): {telegram_buzz_link}\n\n"
                    f"Heuristic screen only — not financial advice. DYOR."
                )
                send_telegram(text)
                alerts_sent += 1
                print(f"  {symbol}: ALERT SENT")
            else:
                print(f"  {symbol}: skip (mcap={market_cap}, lp={lp_locked_pct}, insiders={insider_count})")

        except Exception as e:
            print(f"  {mint}: unexpected error, skipping this token ({type(e).__name__}: {e})")

        time.sleep(0.5)

    save_state(seen)
    print(f"Done. {alerts_sent} alert(s) sent this run.")


if __name__ == "__main__":
    main()
