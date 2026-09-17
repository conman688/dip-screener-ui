"""
Core polling / evaluation logic for the Dip Screener.
No Flask or UI code lives here — app.py wraps this in a background
thread and exposes it over HTTP.

Reworked around one central lesson: a scanner that ANDs together several
"individually reasonable" hard thresholds (min liquidity AND min volume AND
min drawdown AND min bounce AND ...) tends to eliminate almost everything,
even when each threshold alone looks sensible. The fix used throughout this
file is the same one everywhere: gate loosely on basic eligibility, then
SCORE continuously and rank, rather than gating hard on every dimension at
once. See evaluate_token() for the scoring model.
"""

import bisect
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import math

DEXSCREENER_BASE = "https://api.dexscreener.com"

# DexScreener's discovery-oriented endpoints (profiles/boosts) are small,
# fixed-size feeds with no pagination - confirmed against their docs, not
# assumed. There's no true "give me the whole market" endpoint on the free
# API. Broadening discovery here means combining several distinct free
# sources (boosts, top-boosts, profiles, a rotating set of search queries)
# and - most importantly - never forgetting a token once it's been seen, so
# yesterday's find doesn't vanish just because it dropped off a feed today.
#
# None of this is chain-restricted - fetch_latest_boosted/fetch_top_boosted/
# fetch_latest_profiles already return whatever chain DexScreener tracks
# (confirmed live: a search for "pons" turned up chainId "robinhood" pairs
# on Uniswap, no code change needed for that). The launchpad/chain terms
# below (pumpfun/stonkfun/pons, ethereum/weth) exist to make sure those
# specific corners get search-query coverage too, not because discovery was
# ever scoped to one chain - DexScreener's search matches token name/symbol,
# not launchpad, so this is a best-effort widening, not a guarantee.
SEARCH_QUERY_ROTATION = [
    "solana", "pump", "sol", "meme", "moon", "inu", "cat", "dog", "ai",
    "pumpfun", "stonkfun", "pons", "ethereum", "weth",
]


def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "dip-screener/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[http] {url} -> HTTP {e.code}")
    except Exception as e:
        print(f"[http] {url} -> error: {e}")
    return None


def fetch_latest_boosted():
    data = http_get_json(f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
    return data if isinstance(data, list) else []


def fetch_top_boosted():
    """Tokens ranked by active boost count - a different slice than
    'latest', and a free source the previous version never used."""
    data = http_get_json(f"{DEXSCREENER_BASE}/token-boosts/top/v1")
    return data if isinstance(data, list) else []


def fetch_latest_profiles():
    data = http_get_json(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def fetch_search(query: str):
    """Search is the closest thing DexScreener's free API has to a broad
    sweep - it returns PAIR objects directly (not profiles), so results are
    normalized to the same (chainId, tokenAddress) shape as the other
    discovery sources before being merged in."""
    q = urllib.parse.quote(query)
    data = http_get_json(f"{DEXSCREENER_BASE}/latest/dex/search?q={q}")
    pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
    out = []
    for p in pairs or []:
        chain = p.get("chainId")
        addr = (p.get("baseToken") or {}).get("address")
        symbol = (p.get("baseToken") or {}).get("symbol")
        if chain and addr:
            out.append({"chainId": chain, "tokenAddress": addr, "description": symbol})
    return out


def fetch_token_pairs_batch(chain_id: str, token_addresses):
    """Batched market-data fetch - DexScreener's tokens endpoint accepts up
    to 30 comma-separated addresses per call (confirmed against their docs).
    Fetching one token per HTTP request, as the previous version did, is
    exactly the anti-pattern that makes a scanner slow and rate-limit-prone
    once the tracked pool grows past a handful of tokens.

    Returns {token_address: best_pair_dict_or_None}.
    """
    results = {a: None for a in token_addresses}
    if not token_addresses:
        return results
    BATCH_SIZE = 30
    for i in range(0, len(token_addresses), BATCH_SIZE):
        batch = token_addresses[i:i + BATCH_SIZE]
        url = f"{DEXSCREENER_BASE}/tokens/v1/{chain_id}/{','.join(batch)}"
        data = http_get_json(url)
        pairs = data if isinstance(data, list) else []
        # Group returned pairs by base-token address, then keep the
        # highest-liquidity pair per token - same selection rule the
        # single-token code path already used.
        by_addr = {}
        for p in pairs:
            addr = (p.get("baseToken") or {}).get("address")
            if not addr:
                continue
            by_addr.setdefault(addr, []).append(p)
        for addr in batch:
            candidates = by_addr.get(addr) or []
            if candidates:
                candidates.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
                results[addr] = candidates[0]
    return results


def fetch_token_pairs_batch_fallback(chain_id: str, token_address: str):
    """Single-token fallback, used only if a batch call comes back empty for
    a token that a batch response didn't include (e.g. a token with zero
    pairs won't appear in the batch response at all, which is
    indistinguishable from 'the batch call failed' unless we double-check)."""
    url = f"{DEXSCREENER_BASE}/token-pairs/v1/{chain_id}/{token_address}"
    data = http_get_json(url)
    pairs = data if isinstance(data, list) else []
    if not pairs:
        return None
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pairs[0]


RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"


def fetch_rugcheck_report(token_address: str):
    """Bundling/insider-cluster check via RugCheck's free public API -
    confirmed Solana-only (an EVM address returns HTTP 400), so callers
    should skip this for non-Solana chains rather than rely on the error
    path. Returns None on any failure (timeout, rate limit, unknown token,
    etc.) - this is a best-effort enrichment on top of DexScreener data,
    never a required one, so a slow/down third-party API should never be
    able to hide a token, only leave its bundling status "unknown"."""
    url = f"{RUGCHECK_BASE}/tokens/{token_address}/report"
    return http_get_json(url, timeout=12)


def compute_bundling_severity(rugcheck_report):
    """Turns RugCheck's graphInsidersDetected (a count of wallets its graph
    analysis traced back to a common funding source - the same "one wallet
    spoking out to dozens of others" pattern a Bubblemaps-style visual
    shows) into a 0-100 severity score plus a human label, scaled by what
    fraction of the token's total holders that cluster represents. 90
    insider wallets out of 120 total holders is a near-total-bundle
    situation; the same 90 out of 5,000 holders is a much smaller concern -
    the raw count alone can't tell those apart.

    This deliberately does NOT gate eligibility - the brief here is to show
    the user how bad it is and let them decide, not to hide the token.

    Returns None if RugCheck has no usable data (wrong chain, request
    failed, or holder count unknown) - intentionally NOT treated as "0%
    bundled", since that would misrepresent "we don't know" as "confirmed
    clean"."""
    if not rugcheck_report:
        return None
    insiders = rugcheck_report.get("graphInsidersDetected")
    total_holders = rugcheck_report.get("totalHolders")
    if insiders is None or not total_holders:
        return None
    insider_pct = clamp(insiders / total_holders * 100, 0, 100)
    rugged = bool(rugcheck_report.get("rugged"))
    severity = 100.0 if rugged else insider_pct
    if rugged:
        label = "Rugged"
    elif insider_pct >= 40:
        label = "Severe"
    elif insider_pct >= 20:
        label = "High"
    elif insider_pct >= 8:
        label = "Moderate"
    else:
        label = "Low"
    return {
        "source": "rugcheck",
        "insider_wallets": insiders,
        "total_holders": total_holders,
        "insider_pct": round(insider_pct, 1),
        "severity": round(severity, 1),
        "label": label,
        "rugged": rugged,
    }


# GoPlus Security's free Token Security API, used as the EVM-chain
# complement to RugCheck above - confirmed live to support these chain ids,
# including "robinhood" (an EVM chain DexScreener tracks under chainId
# "robinhood", verified against the public chainid.network registry as EVM
# chain 4663 and confirmed live against GoPlus's own supported_chains list).
GOPLUS_CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "optimism": "10",
    "base": "8453",
    "avalanche": "43114",
    "robinhood": "4663",
}


def fetch_goplus_report(chain_id: str, token_address: str):
    """GoPlus token_security lookup for one EVM token - returns None for a
    chain GoPlus doesn't cover, or on any request failure, same best-effort
    contract as fetch_rugcheck_report(). GoPlus keys its result dict by
    lowercased address, hence the .lower() on the way back out."""
    numeric_id = GOPLUS_CHAIN_IDS.get(chain_id)
    if not numeric_id:
        return None
    url = f"https://api.gopluslabs.io/api/v1/token_security/{numeric_id}?contract_addresses={token_address}"
    data = http_get_json(url, timeout=12)
    if not isinstance(data, dict) or data.get("code") != 1:
        return None
    result = data.get("result") or {}
    return result.get(token_address.lower())


def _goplus_flag(entry, key):
    return str(entry.get(key, "0")) == "1"


def compute_evm_risk_severity(entry):
    """EVM-chain complement to compute_bundling_severity() above - GoPlus
    doesn't trace common-funding-wallet clusters the way RugCheck's graph
    analysis does for Solana, so this is a DIFFERENT signal, not the same
    metric reused: unlocked top-10 holder concentration (excluding burned
    tokens and contract/pool addresses - a Uniswap pool holding tokens
    isn't a "whale wallet" in the risk sense) plus a set of contract-level
    red flags (honeypot, mint authority, ownership control, blacklisting,
    pausable transfers, extreme tax). Both correlate with the same "coin
    destined to be dumped on you" outcome the Solana check targets, without
    being able to prove the literal "one wallet funded 95 others" pattern -
    the UI is expected to label this distinctly from the RugCheck result,
    not present it as equivalent.

    Returns None if GoPlus has no usable data (unsupported chain, request
    failed, or holder data missing) - never treated as "0% risk"."""
    if not entry or not entry.get("holder_count"):
        return None

    unlocked_pct = 0.0
    for h in entry.get("holders") or []:
        if str(h.get("is_locked", "0")) == "1":
            continue
        if str(h.get("is_contract", "0")) == "1":
            continue
        addr = (h.get("address") or "").lower()
        if "dead" in addr or addr == "0x0000000000000000000000000000000000000000":
            continue
        try:
            unlocked_pct += float(h.get("percent") or 0) * 100
        except (TypeError, ValueError):
            continue

    # Critical: any single one of these makes the token effectively
    # unsellable or untrustworthy regardless of holder concentration.
    critical_flags = [
        ("is_honeypot", "honeypot - can't sell"),
        ("cannot_sell_all", "can't sell full balance"),
        ("cannot_buy", "buying blocked"),
        ("is_blacklisted", "owner can blacklist wallets"),
        ("selfdestruct", "contract can self-destruct"),
        ("transfer_pausable", "transfers can be paused"),
    ]
    # Soft: not fatal alone, but each is a lever a bad actor could pull
    # later - contributes partial severity rather than an instant "Severe".
    soft_flags = [
        ("is_mintable", "supply can be minted"),
        ("is_proxy", "contract is upgradeable"),
        ("hidden_owner", "hidden owner address"),
        ("can_take_back_ownership", "ownership can be reclaimed"),
        ("owner_change_balance", "owner can change balances"),
        ("slippage_modifiable", "tax/slippage can be changed"),
        ("personal_slippage_modifiable", "per-wallet tax can be set"),
        ("anti_whale_modifiable", "anti-whale limits can be changed"),
        ("trading_cooldown", "trading cooldown enabled"),
    ]

    triggered = [label for key, label in critical_flags if _goplus_flag(entry, key)]
    soft_triggered = [label for key, label in soft_flags if _goplus_flag(entry, key)]

    def _tax_pct(key):
        try:
            return float(entry.get(key) or 0) * 100
        except (TypeError, ValueError):
            return 0.0
    buy_tax, sell_tax = _tax_pct("buy_tax"), _tax_pct("sell_tax")
    if buy_tax >= 10 or sell_tax >= 10:
        triggered.append(f"high tax (buy {buy_tax:.0f}% / sell {sell_tax:.0f}%)")

    if triggered:
        severity = 100.0
        label = "Severe"
    else:
        severity = clamp(unlocked_pct + len(soft_triggered) * 8, 0, 100)
        if severity >= 40:
            label = "Severe"
        elif severity >= 20:
            label = "High"
        elif severity >= 8:
            label = "Moderate"
        else:
            label = "Low"

    return {
        "source": "goplus",
        "unlocked_top10_pct": round(unlocked_pct, 1),
        "flags": triggered + soft_triggered,
        "severity": round(severity, 1),
        "label": label,
        "holder_count": entry.get("holder_count"),
    }


def send_telegram(bot_token: str, chat_id: str, message: str):
    if not bot_token or not chat_id:
        print(f"[alert - telegram not configured]\n{message}\n")
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"[telegram] failed to send: {e}")
        return False


def prune_history(points, window_hours, now):
    cutoff = now - window_hours * 3600
    return [p for p in points if p["t"] >= cutoff]


# Fixed lookback windows used to compute short-horizon drawdown/bounce
# figures from the SAME stored price history used for the overall
# max_drawdown_pct - no extra API calls, just slicing what's already saved
# on every poll.
DIP_WINDOWS = [
    ("15m", 0.25),
    ("30m", 0.5),
    ("1h", 1),
    ("6h", 6),
    ("12h", 12),
    ("24h", 24),
]


def compute_window_metrics(history, current, now):
    """Per fixed lookback window: the high/low WITHIN that window only (not
    all-time), and the resulting drawdown-from-window-high /
    bounce-from-window-low. This is what actually answers "did it dip in
    the last hour / 6 hours / etc", which the single all-time
    high-to-current max_drawdown_pct can't - a token can be up big over 24h
    while still showing a sharp 1h/6h pullback, and that's exactly the
    setup this is meant to surface.

    history is assumed sorted ascending by "t" (true here - always
    appended in time order and pruned without reordering), so each window
    is a binary search plus a slice rather than a full rescan.

    has_full_coverage is False when the oldest stored sample doesn't reach
    back far enough to fill the window yet (token just started being
    tracked) - callers use this to tell "no dip" apart from "not enough
    data yet" instead of silently treating both the same.
    """
    ts = [p["t"] for p in history]
    prices = [p["price"] for p in history]
    oldest_ts = ts[0] if ts else now

    windows = {}
    for label, hours in DIP_WINDOWS:
        cutoff = now - hours * 3600
        idx = bisect.bisect_left(ts, cutoff)
        window_prices = prices[idx:] or [current]
        w_high = max(window_prices)
        w_low = min(window_prices)
        windows[label] = {
            "high": w_high,
            "low": w_low,
            "drawdown_pct": round(((w_high - current) / w_high * 100) if w_high > 0 else 0, 1),
            "bounce_pct": round(((current - w_low) / w_low * 100) if w_low > 0 else 0, 1),
            "has_full_coverage": oldest_ts <= cutoff,
        }
    return windows


def discover_candidates(auto_discover, cycle_count):
    """Combines every free DexScreener discovery source into one pool.
    Returns a list of (chain_id, token_address, label) - NOT yet deduped
    against the watchlist or the persistent known-universe; that happens in
    build_tracked_pool().

    The search-query rotation means a different query is tried each cycle
    rather than all of them every time, spreading load across polls instead
    of bursting every source on every single cycle.
    """
    found = []
    if not auto_discover:
        return found
    for item in fetch_latest_boosted():
        chain, addr = item.get("chainId"), item.get("tokenAddress")
        if chain and addr:
            found.append((chain, addr, item.get("description") or addr[:8]))
    for item in fetch_top_boosted():
        chain, addr = item.get("chainId"), item.get("tokenAddress")
        if chain and addr:
            found.append((chain, addr, item.get("description") or addr[:8]))
    for item in fetch_latest_profiles():
        chain, addr = item.get("chainId"), item.get("tokenAddress")
        if chain and addr:
            found.append((chain, addr, item.get("description") or addr[:8]))
    if SEARCH_QUERY_ROTATION:
        query = SEARCH_QUERY_ROTATION[cycle_count % len(SEARCH_QUERY_ROTATION)]
        for item in fetch_search(query):
            chain, addr = item.get("chainId"), item.get("tokenAddress")
            if chain and addr:
                found.append((chain, addr, item.get("description") or addr[:8]))
    return found


def build_tracked_pool(watchlist, discovered, known_universe, retention_days=14):
    """Merges the watchlist, this cycle's freshly discovered tokens, and the
    PERSISTENT known-universe (tokens discovered on any previous cycle) into
    one deduplicated pool.

    known_universe: dict of "chain:address" -> {label, first_seen, last_seen, ...}
    (this is what makes yesterday's find still show up today even if it
    dropped off every discovery feed in the meantime - it's read from and
    written back to state.json by app.py, not held here.)

    Returns (deduped_pool, funnel_counts) where funnel_counts records how
    many tokens were seen at each stage, for the Scan Coverage panel.
    """
    funnel = {"watchlist": len(watchlist), "discovered_this_cycle": len(discovered)}

    now = time.time()
    cutoff = now - retention_days * 86400
    carried_forward = [
        (k.split(":", 1)[0], k.split(":", 1)[1], v.get("label", k[:8]))
        for k, v in known_universe.items()
        if v.get("last_seen", 0) >= cutoff
    ]
    funnel["carried_forward_from_history"] = len(carried_forward)

    pool = list(watchlist) + list(discovered) + carried_forward
    seen = set()
    deduped = []
    for c, a, l in pool:
        k = (c, a)
        if k not in seen:
            seen.add(k)
            deduped.append((c, a, l))
    funnel["after_dedup"] = len(deduped)
    return deduped, funnel


def passes_eligibility(meta, cfg):
    """Loose, cheap sanity gate applied BEFORE scoring - this is intentionally
    much looser than the old hard-filter approach. Its only job is to drop
    obvious dust/dead pools before spending effort scoring them, not to
    decide what's a good dip. Returns (passes: bool, reason: str|None)."""
    if meta.get("current_price", 0) <= 0:
        return False, "no price"
    liq = meta.get("liquidity_usd", 0)
    vol = meta.get("volume24h_usd", 0)
    vol_5m = meta.get("volume_5m_usd", 0)
    buys_5m = meta.get("buys_m5", 0)
    mcap = meta.get("market_cap_usd", 0)
    min_liq = cfg.get("eligibility_min_liquidity_usd", 2000)
    min_vol = cfg.get("eligibility_min_volume_24h_usd", 500)
    min_vol_5m = cfg.get("eligibility_min_volume_5m_usd", 5000)
    mcap_floor = cfg.get("eligibility_min_market_cap_usd", 10000)
    mcap_ceiling = cfg.get("eligibility_max_market_cap_usd", 100_000_000)
    if liq < min_liq:
        return False, "liquidity below eligibility floor"
    if vol < min_vol:
        return False, "volume below eligibility floor"
    # A deep drawdown with no real trading behind it is a dead pool, not a
    # dip - this is the specific "5m/1h volume but no buy volume" complaint
    # that a 24h-volume floor alone doesn't catch, since a token can coast
    # on stale 24h volume for hours after trading has actually stopped.
    if vol_5m < min_vol_5m:
        return False, "5-minute volume below eligibility floor"
    if buys_5m <= 0:
        return False, "no buy activity in the last 5 minutes"
    if mcap and mcap < mcap_floor:
        return False, "market cap below eligibility floor"
    if mcap and mcap > mcap_ceiling:
        return False, "market cap above eligibility ceiling"
    return True, None


def evaluate_token(history, meta, cfg, now, all_time_high_price=None):
    """
    Scores a token 0-100 as a dip-recovery candidate instead of gating it
    through a chain of hard thresholds. Always returns a full breakdown, even
    for a low-scoring token, so the UI can show WHY something scored the way
    it did instead of just a pass/fail badge.

    Drawdown is computed from the highest price we have ever seen for this
    token (all_time_high_price, tracked persistently in state.json and never
    reset - see app.py), falling back to the rolling high within our own
    retained history window if we don't have a longer-lived high yet, and
    further falling back to DexScreener's own 24h change as a same-cycle
    proxy for a token we've just started tracking. This directly targets the
    "+35% on 24h change but actually 45% below its own recent peak" case a
    naive 24h-change filter misses entirely.
    """
    age_hours = (now - meta.get("pair_created_at", now)) / 3600
    prices = [p["price"] for p in history] or [meta.get("current_price", 0)]
    current = prices[-1] if prices else meta.get("current_price", 0)
    rolling_high = max(prices) if prices else current
    rolling_low = min(prices) if prices else current

    reference_high = max(all_time_high_price or 0, rolling_high)
    used_24h_proxy = False
    if not reference_high or reference_high <= current:
        # No persisted all-time-high yet (a token we've only just started
        # tracking) and no local history either - reconstruct an approximate
        # recent peak from DexScreener's own m5/h1/h6/h24 change windows by
        # inferring what the price was at each checkpoint and taking the
        # highest. This only catches a peak that happened within the last
        # 24h - a slower, multi-day "ran up last week, pulling back since"
        # pattern (the advice's own worked example) is NOT reconstructable
        # from a handful of percentage changes; that case is exactly why
        # all_time_high_price is persisted across polls once we've tracked a
        # token for a while, which this proxy is only a same-day stand-in for.
        checkpoints = [current]
        for key in ("price_change_m5_pct", "price_change_h1_pct", "price_change_h6_pct", "price_change_24h_pct"):
            change = meta.get(key)
            if change is not None and change != -100:
                implied = current / (1 + change / 100)
                if implied > 0:
                    checkpoints.append(implied)
        proxy_high = max(checkpoints)
        if proxy_high > current:
            reference_high = proxy_high
            used_24h_proxy = True
        else:
            reference_high = current

    max_drawdown_pct = ((reference_high - current) / reference_high * 100) if reference_high > 0 else 0
    bounce_pct = ((current - rolling_low) / rolling_low * 100) if rolling_low > 0 else 0

    windows = compute_window_metrics(history, current, now)
    # Sharpest pullback across the short-horizon windows - the "flash dip"
    # signal max_drawdown_pct alone can't see, e.g. a token up 45% over 12h
    # that just dropped 19% in the last hour.
    short_term_dip_pct = max(windows[w]["drawdown_pct"] for w in ("15m", "30m", "1h"))
    short_term_dip_has_coverage = any(windows[w]["has_full_coverage"] for w in ("15m", "30m", "1h"))

    confirm_polls = cfg.get("confirm_polls", 3)
    recent = prices[-confirm_polls:]
    bounce_confirmed = len(recent) >= confirm_polls and all(
        recent[i] <= recent[i + 1] for i in range(len(recent) - 1)
    )

    liquidity = meta.get("liquidity_usd", 0)
    volume = meta.get("volume24h_usd", 0)
    market_cap = meta.get("market_cap_usd", 0)
    buys = meta.get("buys_h1", 0)
    sells = meta.get("sells_h1", 0)

    # ---- Component scores (0-100 each), weighted:
    # 25% drawdown quality, 20% short-term dip quality (the 15m/30m/1h
    # flash-dip signal - the main "catch it as it dips" lever), 10% volume
    # retention, 15% liquidity, 5% buy/sell momentum, 10% transaction
    # activity, 10% narrative/legitimacy presence (see note below), 5% token
    # age.
    #
    # Buy/sell momentum is deliberately a small weight, not a large one: a
    # token that's actively dipping will naturally be sell-heavy (that's
    # what a dip IS), and a high buy ratio more often means it's already
    # started bouncing. This tool is meant to surface a dip as it's
    # happening, not after recovery buying has kicked in, so momentum isn't
    # allowed to drag the score down much just because a fresh dip is
    # sell-dominated - see also the eligibility floor in
    # passes_eligibility(), which requires SOME buy activity in the last 5
    # minutes just to rule out a fully dead/abandoned pool.

    # Drawdown quality: rewards a real pullback (20-70% range is the sweet
    # spot this tool is looking for), but does NOT keep rewarding deeper and
    # deeper drawdown without limit - a token down 95% is more likely dead
    # than "due for a bounce," which is the dead-vs-dip distinction the
    # advice specifically called out.
    if max_drawdown_pct < 15:
        drawdown_score = max_drawdown_pct / 15 * 40  # barely pulled back yet
    elif max_drawdown_pct <= 70:
        drawdown_score = 40 + (max_drawdown_pct - 15) / 55 * 60  # the sweet spot
    else:
        # Beyond 70% drawdown, treat further depth as a fading signal rather
        # than a growing one - this is what actually encodes "probably dying"
        # instead of "even more of a bargain."
        drawdown_score = max(0, 100 - (max_drawdown_pct - 70) * 2)

    # Volume retention: compares current volume against this token's own
    # historical average volume (from our tracked history), not an absolute
    # dollar bar - a token that's kept trading is a different thing from one
    # whose volume has collapsed to nothing, even at the same drawdown depth.
    hist_volumes = [p.get("volume") for p in history if p.get("volume")]
    avg_volume = (sum(hist_volumes) / len(hist_volumes)) if hist_volumes else volume
    volume_ratio = (volume / avg_volume) if avg_volume > 0 else 1
    volume_score = clamp(volume_ratio * 50, 0, 100)

    liquidity_score = clamp(_log_scale(liquidity, ceiling=2_000_000) * 100, 0, 100)

    total_tx = buys + sells
    buy_ratio = (buys / total_tx) if total_tx > 0 else 0.5
    momentum_score = clamp((buy_ratio - 0.3) / 0.4 * 100, 0, 100)

    activity_score = clamp(_log_scale(total_tx, ceiling=200) * 100, 0, 100)

    # Short-term dip quality: same sweet-spot shape as drawdown_score, but
    # tuned to short-horizon magnitudes and centered on the ~40% flash-dip
    # this tool is specifically looking for (5-45% sweet spot, peaking at
    # 45% then fading past that - a near-total collapse inside an hour reads
    # as a rug/dead pool, not a buyable dip).
    if short_term_dip_pct < 5:
        short_term_dip_score = short_term_dip_pct / 5 * 40
    elif short_term_dip_pct <= 45:
        short_term_dip_score = 40 + (short_term_dip_pct - 5) / 40 * 60
    else:
        short_term_dip_score = max(0, 100 - (short_term_dip_pct - 45) * 2)
    short_term_dip_score = clamp(short_term_dip_score, 0, 100)

    # Narrative/legitimacy presence: built entirely from data already
    # pulled on every market-data fetch (DexScreener's info.websites /
    # info.socials - no extra API calls) as a cheap proxy for "does this
    # look like a real project with a community," not a measure of actual
    # social-media virality/hype - detecting whether something is genuinely
    # trending right now would need a paid social-listening API this tool
    # doesn't have. A real early-stage project without a filled-in bio, or
    # a wildly viral meme that never bothered with a website, will both
    # slip through this heuristic; treat it as one more data point, not a
    # verdict. This replaces the old "holder behavior" placeholder, which
    # was always a fabricated neutral 50 - this is honest (if imperfect)
    # real data instead of a fake stand-in.
    websites = meta.get("websites") or []
    socials = meta.get("socials") or []
    social_types = {s.get("type") for s in socials if isinstance(s, dict) and s.get("type")}
    narrative_points = (1 if websites else 0) + min(len(social_types), 3)
    if narrative_points >= 4:
        narrative_label = "Strong"
    elif narrative_points >= 2:
        narrative_label = "Some"
    elif narrative_points >= 1:
        narrative_label = "Minimal"
    else:
        narrative_label = "None"
    narrative_score = clamp(narrative_points / 4 * 100, 0, 100)
    narrative = {
        "label": narrative_label,
        "has_website": bool(websites),
        "has_twitter": "twitter" in social_types,
        "has_telegram": "telegram" in social_types,
        "has_discord": "discord" in social_types,
        "social_count": len(social_types),
    }

    age_score = clamp(age_hours / (7 * 24) * 100, 0, 100)  # maxes out at 7 days old

    score = round(
        drawdown_score * 0.25 +
        short_term_dip_score * 0.20 +
        volume_score * 0.10 +
        liquidity_score * 0.15 +
        momentum_score * 0.05 +
        activity_score * 0.10 +
        narrative_score * 0.10 +
        age_score * 0.05
    )

    return {
        "label": meta.get("label", "?"),
        "url": meta.get("url", ""),
        "current_price": current,
        "reference_high": reference_high,
        "reference_high_is_proxy": used_24h_proxy,
        "rolling_low": rolling_low,
        "market_cap_usd": market_cap,
        "max_drawdown_pct": round(max_drawdown_pct, 1),
        "bounce_pct": round(bounce_pct, 1),
        "bounce_confirmed": bounce_confirmed,
        "windows": windows,
        "short_term_dip_pct": round(short_term_dip_pct, 1),
        "short_term_dip_has_coverage": short_term_dip_has_coverage,
        # Top-level copy of windows["1h"]["bounce_pct"] (current vs the 1h
        # window's own low, not the 7-day rolling low bounce_pct above) -
        # flattened out here so the UI can sort/display it without reaching
        # into the nested windows dict.
        "bounce_1h_pct": windows["1h"]["bounce_pct"],
        "bounce_1h_has_coverage": windows["1h"]["has_full_coverage"],
        "age_hours": round(age_hours, 1),
        "liquidity_usd": liquidity,
        "volume24h_usd": volume,
        "volume_5m_usd": meta.get("volume_5m_usd", 0),
        "narrative": narrative,
        "narrative_score": round(narrative_score),
        "score": score,
        "score_breakdown": {
            "drawdown_quality": round(drawdown_score),
            "short_term_dip": round(short_term_dip_score),
            "volume_retention": round(volume_score),
            "liquidity": round(liquidity_score),
            "buy_sell_momentum": round(momentum_score),
            "tx_activity": round(activity_score),
            "narrative_presence": round(narrative_score),
            "age": round(age_score),
        },
        "points_tracked": len(history),
        "last_updated": now,
    }


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _log_scale(value, ceiling):
    if value <= 0:
        return 0
    return clamp(math.log10(max(value, 1)) / math.log10(max(ceiling, 10)), 0, 1)


def format_alert(key, status):
    proxy_note = " (approx., limited history so far)" if status.get("reference_high_is_proxy") else ""
    short_term_note = "" if status.get("short_term_dip_has_coverage") else " (limited history so far)"
    bundling = status.get("bundling")
    # Only shown when a bundling/risk check actually has data - silence
    # rather than a misleading "unknown = fine" line for everything else.
    # Two shapes depending on chain (see compute_bundling_severity vs
    # compute_evm_risk_severity) - format each accordingly.
    bundling_line = ""
    if bundling and bundling.get("source") == "goplus":
        flags_note = f" — {', '.join(bundling['flags'])}" if bundling.get("flags") else ""
        bundling_line = (
            f"Risk (GoPlus): {bundling['label']} — {bundling['unlocked_top10_pct']}% held by "
            f"unlocked top-10 wallets{flags_note}\n"
        )
    elif bundling:
        bundling_line = (
            f"Bundling: {bundling['label']} — {bundling['insider_pct']}% of holders "
            f"({bundling['insider_wallets']}/{bundling['total_holders']}) traced to a common funder\n"
        )
    return (
        f"\U0001F7E2 *Dip candidate*: {status['label']} — score {status['score']}/100\n"
        f"Sharpest short-term pullback (15m/30m/1h): -{status['short_term_dip_pct']}%{short_term_note}\n"
        f"Down {status['max_drawdown_pct']}% from its recent high{proxy_note} "
        f"(+{status['bounce_1h_pct']}% off the 1h low).\n"
        f"{bundling_line}"
        f"Price: ${status['current_price']:.8f}  |  Age: {status['age_hours']}h\n"
        f"Liquidity: ${status['liquidity_usd']:,.0f}  |  5m Vol: ${status['volume_5m_usd']:,.0f}  |  24h Vol: ${status['volume24h_usd']:,.0f}\n"
        f"{status['url']}"
    )
