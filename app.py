"""
Dip Screener — local web dashboard
===================================
Run this, then open http://127.0.0.1:5050 in your browser.

Everything runs locally on your own laptop: the Flask server, the
background polling thread, and the config file. Nothing is sent
anywhere except (a) requests to DexScreener's public API to read
prices, and (b) a Telegram message when a token scores high enough,
if you've set that up.

    pip install -r requirements.txt
    python app.py
"""

import json
import os
import threading
import time
import uuid

from flask import Flask, jsonify, request, render_template

import screener_core as core

app = Flask(__name__)
# Otherwise Jinja caches the compiled template in memory the first time it's
# rendered (auto-reload defaults to following app.debug, which is False
# here) and ignores later edits to templates/index.html until the process
# restarts.
app.config["TEMPLATES_AUTO_RELOAD"] = True

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

DEFAULT_CONFIG = {
    "watchlist": [],  # list of [chain_id, token_address, label]
    "auto_discover": True,

    # Loose eligibility gate - applied BEFORE scoring, deliberately wide so
    # it excludes obvious dust/dead pools without eliminating real
    # candidates the way a chain of hard AND'd thresholds does. These are
    # NOT the same knobs as the old dip/recovery/liquidity/volume filters -
    # those are now scoring inputs (see screener_core.evaluate_token), not
    # pass/fail gates.
    "eligibility_min_liquidity_usd": 2000,
    "eligibility_min_volume_24h_usd": 500,
    # Requires real, CURRENT trading, not just stale 24h volume - a pool
    # can coast on a big number from hours ago long after activity has
    # actually died off. Paired with the "at least one buy in the last 5
    # minutes" check in screener_core.passes_eligibility().
    "eligibility_min_volume_5m_usd": 5000,
    "eligibility_min_market_cap_usd": 10000,
    "eligibility_max_market_cap_usd": 100_000_000,

    "min_age_hours": 0,
    "max_age_hours": 0,
    "min_market_cap_usd": 0,
    "max_market_cap_usd": 0,
    "confirm_polls": 3,
    "min_liquidity_usd": 0,
    "min_volume_24h_usd": 0,

    "alert_score_threshold": 70,
    "known_universe_retention_days": 14,

    # Bundling/insider-cluster check (RugCheck, Solana only) - informational
    # only, never blocks a token from showing up; see run_cycle() and
    # screener_core.compute_bundling_severity(). Cached per-token for
    # bundling_recheck_hours since holder-cluster structure doesn't shift
    # cycle to cycle, so this isn't worth re-querying every 90 seconds.
    "bundling_check_enabled": True,
    "bundling_recheck_hours": 6,

    "poll_interval_seconds": 90,  # tight enough to resolve real 15m/30m windows (see DIP_WINDOWS)
    "history_window_hours": 168,  # 7 days - long enough to catch multi-day retracements, not just same-day
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "running": True,
}

lock = threading.Lock()

# ---------------- Paper trading ----------------
# Manual-only buy-the-dip trainer: you pick the entry ($ amount, on any
# tracked row) and the exit (Sell, at the current price) - nothing here
# auto-buys or auto-sells. Positions/trade history persist in state.json
# under "paper_portfolio" the same way watchlist/known_universe do.

PAPER_STARTING_BALANCE = 10000


def new_paper_portfolio():
    # A fresh dict every call - state.setdefault(...) below would otherwise
    # share the same "positions"/"closed_trades" containers across every
    # call site that constructs a default, corrupting state.
    return {
        "starting_balance": PAPER_STARTING_BALANCE,
        "cash_usd": PAPER_STARTING_BALANCE,
        "positions": {},
        "closed_trades": [],
    }


def latest_price_for_key(key):
    """Best-effort latest known price for a tracked token, used by
    paper-trade buy/sell/valuation - prefers this cycle's live status,
    falls back to the most recent stored history sample (covers a token
    that temporarily dropped below the eligibility floor and isn't in
    live_status this cycle). Caller must already hold `lock`."""
    status = live_status.get(key)
    if status and status.get("current_price"):
        return status["current_price"]
    history = state.get("history", {}).get(key) or []
    if history:
        return history[-1].get("price")
    return None


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


with lock:
    config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
    state = load_json(STATE_FILE, {"history": {}, "alerted": {}, "known_universe": {}})
    state.setdefault("known_universe", {})
    state.setdefault("paper_portfolio", new_paper_portfolio())

# in-memory, rebuilt every cycle — not persisted (derived data)
live_status = {}   # key -> status dict from evaluate_token
alerts_feed = []    # list of {id, key, label, message, ts}
last_cycle_ts = 0
next_alert_id = 1
last_funnel = {}
cycle_count = 0


def run_cycle():
    global last_cycle_ts, next_alert_id, last_funnel, cycle_count

    with lock:
        cfg_snapshot = dict(config)
        watchlist = [tuple(x) for x in cfg_snapshot.get("watchlist", [])]
        known_universe_snapshot = dict(state.get("known_universe", {}))
        this_cycle = cycle_count
        cycle_count += 1

    if not cfg_snapshot.get("running", True):
        return

    now = time.time()

    discovered = core.discover_candidates(cfg_snapshot.get("auto_discover", True), this_cycle)
    pool, funnel = core.build_tracked_pool(
        watchlist, discovered, known_universe_snapshot,
        retention_days=cfg_snapshot.get("known_universe_retention_days", 14),
    )

    # Batch market-data fetches, grouped by chain (DexScreener's batch
    # endpoint is chain-scoped, up to 30 addresses per call) instead of one
    # HTTP request per token.
    by_chain = {}
    for chain_id, token_address, label in pool:
        by_chain.setdefault(chain_id, []).append((token_address, label))

    fetched = {}  # key -> (pair_dict, label)
    for chain_id, items in by_chain.items():
        addrs = [a for a, _ in items]
        batch_result = core.fetch_token_pairs_batch(chain_id, addrs)
        for token_address, label in items:
            pair = batch_result.get(token_address)
            if pair is None:
                # Batch response omitted this token entirely (rather than
                # explicitly erroring) - double-check with a single-token
                # call before giving up on it, since an omission is
                # ambiguous between "no pairs exist" and "not in this batch."
                pair = core.fetch_token_pairs_batch_fallback(chain_id, token_address)
            if pair is not None:
                fetched[f"{chain_id}:{token_address}"] = (pair, label, chain_id, token_address)

    funnel["fetched_market_data"] = len(fetched)

    eligible_count = 0
    new_status = {}
    known_universe_updates = {}

    for key, (best, label, chain_id, token_address) in fetched.items():
        try:
            price = float(best.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue

        pair_created_ms = best.get("pairCreatedAt")
        market_cap = float(best.get("marketCap") or best.get("fdv") or 0)
        txns_h1 = (best.get("txns") or {}).get("h1") or {}
        txns_m5 = (best.get("txns") or {}).get("m5") or {}
        meta = {
            "label": (best.get("baseToken") or {}).get("symbol", label),
            "url": best.get("url", ""),
            "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
            "volume24h_usd": float((best.get("volume") or {}).get("h24") or 0),
            "volume_5m_usd": float((best.get("volume") or {}).get("m5") or 0),
            "market_cap_usd": market_cap,
            "pair_created_at": (pair_created_ms / 1000) if pair_created_ms else now,
            "current_price": price,
            "price_change_m5_pct": (best.get("priceChange") or {}).get("m5"),
            "price_change_h1_pct": (best.get("priceChange") or {}).get("h1"),
            "price_change_h6_pct": (best.get("priceChange") or {}).get("h6"),
            "price_change_24h_pct": (best.get("priceChange") or {}).get("h24"),
            "buys_m5": float(txns_m5.get("buys") or 0),
            "sells_m5": float(txns_m5.get("sells") or 0),
            "buys_h1": float(txns_h1.get("buys") or 0),
            "sells_h1": float(txns_h1.get("sells") or 0),
        }

        eligible, reason = core.passes_eligibility(meta, cfg_snapshot)
        if not eligible or price <= 0:
            continue
        eligible_count += 1

        with lock:
            history = state["history"].get(key, [])
            history.append({"t": now, "price": price, "volume": meta["volume24h_usd"]})
            history = core.prune_history(history, cfg_snapshot.get("history_window_hours", 168), now)
            state["history"][key] = history

        prior = known_universe_snapshot.get(key, {})
        all_time_high_price = prior.get("all_time_high_price")

        # Bundling/insider-cluster check - only for the shortlist of tokens
        # that already cleared eligibility (not the full ~150-token pool),
        # and cached per-token so a token already checked recently doesn't
        # cost another RugCheck call every single cycle.
        bundling = prior.get("bundling")
        bundling_checked_at = prior.get("bundling_checked_at", 0)
        recheck_seconds = cfg_snapshot.get("bundling_recheck_hours", 6) * 3600
        if (
            cfg_snapshot.get("bundling_check_enabled", True)
            and chain_id == "solana"
            and (now - bundling_checked_at > recheck_seconds)
        ):
            report = core.fetch_rugcheck_report(token_address)
            bundling = core.compute_bundling_severity(report)
            bundling_checked_at = now

        known_universe_updates[key] = {
            "label": meta["label"],
            "first_seen": prior.get("first_seen", now),
            "last_seen": now,
            "all_time_high_price": max(all_time_high_price or 0, price) or price,
            "all_time_high_mcap": max(prior.get("all_time_high_mcap", 0) or 0, market_cap),
            "bundling": bundling,
            "bundling_checked_at": bundling_checked_at,
        }

        status = core.evaluate_token(history, meta, cfg_snapshot, now, all_time_high_price)
        status["platform"] = best.get("dexId", "")
        status["bundling"] = bundling
        # Flattened copy for the UI's sortable column - bundling itself is
        # a nested dict (or None when unavailable), which client-side sort
        # can't key off of directly.
        status["bundling_severity"] = bundling["severity"] if bundling else -1
        new_status[key] = status

        if status["score"] >= cfg_snapshot.get("alert_score_threshold", 70):
            with lock:
                last_alert_ts = state["alerted"].get(key, 0)
                should_alert = now - last_alert_ts > 6 * 3600
                if should_alert:
                    state["alerted"][key] = now

            if should_alert:
                message = core.format_alert(key, status)
                core.send_telegram(
                    cfg_snapshot.get("telegram_bot_token", ""),
                    cfg_snapshot.get("telegram_chat_id", ""),
                    message,
                )
                with lock:
                    alerts_feed.insert(0, {
                        "id": next_alert_id,
                        "key": key,
                        "label": status["label"],
                        "message": message,
                        "ts": now,
                    })
                    next_alert_id += 1
                    del alerts_feed[50:]  # keep feed bounded

    funnel["passed_eligibility"] = eligible_count
    funnel["scored"] = len(new_status)
    funnel["alert_threshold_met"] = sum(
        1 for s in new_status.values() if s["score"] >= cfg_snapshot.get("alert_score_threshold", 70)
    )

    with lock:
        live_status.clear()
        live_status.update(new_status)
        state["known_universe"].update(known_universe_updates)
        # Prune known_universe entries not seen recently, so state.json
        # doesn't grow forever - this is the same retention window used to
        # decide what gets carried forward into the tracked pool.
        cutoff = now - cfg_snapshot.get("known_universe_retention_days", 14) * 86400
        state["known_universe"] = {
            k: v for k, v in state["known_universe"].items() if v.get("last_seen", 0) >= cutoff
        }
        save_json(STATE_FILE, state)
        last_funnel = funnel
    last_cycle_ts = now


def background_loop():
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[cycle error] {e}")
        with lock:
            interval = config.get("poll_interval_seconds", 300)
        time.sleep(max(10, interval))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with lock:
        watched = {(c, a) for c, a, _ in config.get("watchlist", [])}
        tokens = []
        for k, v in live_status.items():
            chain_id, token_address = k.split(":", 1)
            tokens.append({
                "key": k,
                "chain_id": chain_id,
                "token_address": token_address,
                "is_watchlisted": (chain_id, token_address) in watched,
                **v,
            })
        running = config.get("running", True)
        interval = config.get("poll_interval_seconds", 300)
        funnel = dict(last_funnel)
    tokens.sort(key=lambda t: -t["score"])
    return jsonify({
        "tokens": tokens,
        "running": running,
        "poll_interval_seconds": interval,
        "last_cycle_ts": last_cycle_ts,
        "server_time": time.time(),
        "funnel": funnel,
    })


@app.route("/api/alerts")
def api_alerts():
    with lock:
        return jsonify({"alerts": list(alerts_feed)})


@app.route("/api/config", methods=["GET"])
def api_get_config():
    with lock:
        return jsonify(config)


@app.route("/api/config", methods=["POST"])
def api_set_config():
    payload = request.get_json(force=True, silent=True) or {}
    with lock:
        for k in DEFAULT_CONFIG:
            if k in payload:
                config[k] = payload[k]
        save_json(CONFIG_FILE, config)
    return jsonify({"ok": True, "config": config})


@app.route("/api/watchlist", methods=["GET"])
def api_get_watchlist():
    with lock:
        return jsonify({"watchlist": config.get("watchlist", [])})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    """Adds one token to the permanent watchlist and saves immediately —
    this is what powers the one-click star button on a token row, so
    watching something you're already looking at never requires opening
    Settings or retyping its address."""
    payload = request.get_json(force=True, silent=True) or {}
    chain_id = (payload.get("chain_id") or "").strip()
    token_address = (payload.get("token_address") or "").strip()
    label = (payload.get("label") or "").strip() or token_address[:8]
    if not chain_id or not token_address:
        return jsonify({"ok": False, "error": "chain_id and token_address are required"}), 400
    with lock:
        existing = {(c, a) for c, a, _ in config.get("watchlist", [])}
        if (chain_id, token_address) not in existing:
            config.setdefault("watchlist", []).append([chain_id, token_address, label])
            save_json(CONFIG_FILE, config)
        watchlist = config["watchlist"]
    return jsonify({"ok": True, "watchlist": watchlist})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    payload = request.get_json(force=True, silent=True) or {}
    chain_id = (payload.get("chain_id") or "").strip()
    token_address = (payload.get("token_address") or "").strip()
    with lock:
        config["watchlist"] = [
            row for row in config.get("watchlist", [])
            if not (row[0] == chain_id and row[1] == token_address)
        ]
        save_json(CONFIG_FILE, config)
        watchlist = config["watchlist"]
    return jsonify({"ok": True, "watchlist": watchlist})


@app.route("/api/control", methods=["POST"])
def api_control():
    payload = request.get_json(force=True, silent=True) or {}
    action = payload.get("action")
    with lock:
        if action == "start":
            config["running"] = True
        elif action == "pause":
            config["running"] = False
        elif action == "poll_now":
            pass  # handled below, outside the lock
        save_json(CONFIG_FILE, config)
    if action == "poll_now":
        threading.Thread(target=run_cycle, daemon=True).start()
    return jsonify({"ok": True, "running": config.get("running", True)})


@app.route("/api/paper/portfolio")
def api_paper_portfolio():
    with lock:
        portfolio = state.setdefault("paper_portfolio", new_paper_portfolio())
        positions = []
        market_value = 0.0
        for pos in portfolio["positions"].values():
            price = latest_price_for_key(pos["key"]) or pos["entry_price"]
            value = pos["quantity"] * price
            market_value += value
            unrealized_pnl = value - pos["amount_usd"]
            positions.append({
                **pos,
                "current_price": price,
                "market_value_usd": value,
                "unrealized_pnl_usd": unrealized_pnl,
                "unrealized_pnl_pct": (unrealized_pnl / pos["amount_usd"] * 100) if pos["amount_usd"] > 0 else 0,
            })
        positions.sort(key=lambda p: -p["opened_at"])

        closed = list(portfolio.get("closed_trades", []))
        wins = sum(1 for t in closed if t["pnl_usd"] > 0)

        cash = portfolio["cash_usd"]
        starting_balance = portfolio.get("starting_balance", PAPER_STARTING_BALANCE)
        total_value = cash + market_value

        return jsonify({
            "cash_usd": cash,
            "starting_balance": starting_balance,
            "market_value_usd": market_value,
            "total_value_usd": total_value,
            "total_pnl_usd": total_value - starting_balance,
            "total_pnl_pct": ((total_value - starting_balance) / starting_balance * 100) if starting_balance else 0,
            "realized_pnl_usd": sum(t["pnl_usd"] for t in closed),
            "win_rate_pct": (wins / len(closed) * 100) if closed else None,
            "trade_count": len(closed),
            "positions": positions,
            "closed_trades": closed[:20],
        })


@app.route("/api/paper/buy", methods=["POST"])
def api_paper_buy():
    payload = request.get_json(force=True, silent=True) or {}
    chain_id = (payload.get("chain_id") or "").strip()
    token_address = (payload.get("token_address") or "").strip()
    label = (payload.get("label") or "").strip() or token_address[:8]
    try:
        amount_usd = float(payload.get("amount_usd"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Enter a numeric dollar amount"}), 400
    if not chain_id or not token_address:
        return jsonify({"ok": False, "error": "chain_id and token_address are required"}), 400
    if amount_usd <= 0:
        return jsonify({"ok": False, "error": "Amount must be greater than $0"}), 400

    key = f"{chain_id}:{token_address}"
    with lock:
        portfolio = state.setdefault("paper_portfolio", new_paper_portfolio())
        if amount_usd > portfolio["cash_usd"]:
            return jsonify({"ok": False, "error": f"Only ${portfolio['cash_usd']:,.2f} paper cash available"}), 400
        price = latest_price_for_key(key)
        if not price or price <= 0:
            return jsonify({"ok": False, "error": "No current price available for this token yet - wait for the next scan cycle"}), 400

        trade_id = uuid.uuid4().hex[:12]
        status = live_status.get(key) or {}
        portfolio["cash_usd"] -= amount_usd
        portfolio["positions"][trade_id] = {
            "trade_id": trade_id,
            "key": key,
            "chain_id": chain_id,
            "token_address": token_address,
            "label": label,
            "entry_price": price,
            "amount_usd": amount_usd,
            "quantity": amount_usd / price,
            "opened_at": time.time(),
            "score_at_entry": status.get("score"),
        }
        save_json(STATE_FILE, state)
    return jsonify({"ok": True, "trade_id": trade_id})


@app.route("/api/paper/sell", methods=["POST"])
def api_paper_sell():
    payload = request.get_json(force=True, silent=True) or {}
    trade_id = (payload.get("trade_id") or "").strip()
    with lock:
        portfolio = state.setdefault("paper_portfolio", new_paper_portfolio())
        position = portfolio["positions"].get(trade_id)
        if not position:
            return jsonify({"ok": False, "error": "Position not found - it may already be closed"}), 404
        price = latest_price_for_key(position["key"])
        if not price or price <= 0:
            return jsonify({"ok": False, "error": "No current price available to close this position yet"}), 400

        proceeds = position["quantity"] * price
        pnl_usd = proceeds - position["amount_usd"]
        portfolio["cash_usd"] += proceeds
        del portfolio["positions"][trade_id]

        closed = dict(position)
        closed.update({
            "exit_price": price,
            "closed_at": time.time(),
            "proceeds_usd": proceeds,
            "pnl_usd": pnl_usd,
            "pnl_pct": (pnl_usd / position["amount_usd"] * 100) if position["amount_usd"] > 0 else 0,
        })
        portfolio.setdefault("closed_trades", []).insert(0, closed)
        del portfolio["closed_trades"][200:]  # keep state.json bounded
        save_json(STATE_FILE, state)
    return jsonify({"ok": True})


@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    with lock:
        state["paper_portfolio"] = new_paper_portfolio()
        save_json(STATE_FILE, state)
    return jsonify({"ok": True})


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host="127.0.0.1", port=5050, debug=False)
