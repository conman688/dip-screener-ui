"""
QuickNode real-time layer
=========================
Everything in this file is additive and optional: with no QuickNode
endpoint configured, every public function/class here is a no-op, and
app.py's existing DexScreener-polling pipeline behaves exactly as before.
Nothing here replaces that pipeline - it feeds it faster candidates and
adds a couple of alert paths DexScreener's own REST API can't give you
(a live tripwire, and a Solana wallet-reputation lookup), while price/
liquidity/volume scoring still comes from the same batched DexScreener
calls it always has.

Four independent pieces, each gated by its own config flag:

1. Real-time pool discovery (Solana) - watches pump.fun + Raydium program
   activity over a WebSocket instead of waiting for a token to surface in
   DexScreener's own discovery feeds (which can lag by many poll cycles,
   or miss an obscure launch entirely). A candidate found this way is
   just handed to the SAME eligibility/scoring pipeline every other
   candidate goes through - a wrong or noisy candidate is filtered out
   exactly the way DexScreener's own noisy discovery feeds already are.

2. Creator-wallet reputation (Solana, via Metaplex DAS) - looks up a
   mint's on-chain creator and counts how many other tokens that same
   wallet has created. Best-effort: for a pump.fun launch this relies on
   pump.fun having set the real launching wallet as the token's metadata
   creator (which is how pump.fun's own creator-rewards feature works),
   not something this code can independently verify.

3. Live LP tripwire (Solana) - watches every transaction touching an
   already-tracked pool address and alerts the moment a transaction
   drains a large fraction of its token balance, instead of waiting for
   the next bundling_recheck_hours check.

4. EVM mempool early warning (mainstream chains only: ethereum, base,
   bsc, polygon, arbitrum, optimism, avalanche) - flags a large pending
   sell or liquidity removal aimed at a tracked pool/router address a few
   seconds before it confirms. Deliberately narrow: this does NOT decode
   arbitrary swaps across the whole chain, only activity aimed at
   addresses this tool is already tracking, because subscribing to a
   chain's full pending-transaction firehose and resolving every hash is
   expensive and will burn through request quota fast on a busy chain -
   see the cost note on EvmMempoolFeed.
"""

import json
import threading
import time
import urllib.error
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


# ---------------------------------------------------------------------------
# Shared JSON-RPC helpers (HTTP)
# ---------------------------------------------------------------------------

def _rpc_call(http_url, method, params, timeout=10):
    """One JSON-RPC 2.0 request over HTTP. Returns the `result` value, or
    None on any transport/HTTP/RPC error - callers treat None as "this
    lookup isn't available right now," never as a crash, since every
    feature in this module must degrade to a no-op rather than take down
    the poll cycle."""
    if not http_url:
        return None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        http_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "dip-screener/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    if "error" in data:
        return None
    return data.get("result")


def wss_to_http(wss_url):
    """QuickNode gives you one endpoint URL; the HTTP and WSS forms are the
    same host/path with a different scheme. Accepts either form back."""
    if wss_url.startswith("wss://"):
        return "https://" + wss_url[len("wss://"):]
    if wss_url.startswith("ws://"):
        return "http://" + wss_url[len("ws://"):]
    return wss_url


# ---------------------------------------------------------------------------
# 2. Creator-wallet reputation (Solana, Metaplex DAS)
# ---------------------------------------------------------------------------

def get_creator_reputation(solana_http_url, mint_address, timeout=10):
    """Best-effort "who made this token, and what else have they made"
    lookup via Metaplex DAS (getAsset + getAssetsByCreator). Returns
    {"creator": <wallet>, "other_asset_count": <int>} or None if DAS
    isn't reachable/enabled on this endpoint, the mint isn't indexed, or
    no creator is recorded.

    This is a legitimacy/rug-reputation signal, not a certainty: DAS
    reflects whatever creator address the token's metadata names, and for
    a pump.fun launch that's only meaningful because pump.fun itself sets
    it to the real launching wallet (for its own creator-rewards feature)
    - a token minted some other way may have no creator recorded, or one
    that isn't a person (a program-owned address), and this function has
    no way to tell those cases apart from "n/a."
    """
    asset = _rpc_call(solana_http_url, "getAsset", {"id": mint_address}, timeout=timeout)
    if not asset:
        return None
    creators = asset.get("creators") or []
    creator = next((c.get("address") for c in creators if c.get("address")), None)
    if not creator:
        return None

    other = _rpc_call(
        solana_http_url,
        "getAssetsByCreator",
        {"creatorAddress": creator, "onlyVerified": False, "page": 1, "limit": 1000},
        timeout=timeout,
    )
    total = (other or {}).get("total")
    if total is None:
        items = (other or {}).get("items") or []
        total = len(items)
    return {"creator": creator, "other_asset_count": total}


# ---------------------------------------------------------------------------
# 1 & 3. Solana real-time feed: pool discovery + LP tripwire
# ---------------------------------------------------------------------------

# Well-known Solana program IDs this watches. Configurable (see
# app.py's DEFAULT_CONFIG) rather than hardcoded only, since getting one
# of these wrong should be a config fix, not a code change - and this
# feed fails safe either way: a wrong program ID just means it never
# fires, not that anything breaks.
DEFAULT_LAUNCH_PROGRAM_IDS = {
    "pumpfun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "raydium_amm_v4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
}


class SolanaRealtimeFeed:
    """One persistent WebSocket connection multiplexing:
      - logsSubscribe(mentions=[launch program]) for new-pool discovery
      - logsSubscribe(mentions=[pool address]) per tracked pool, for the
        LP tripwire

    Runs in its own daemon thread with automatic reconnect. Talks back to
    app.py purely through the two callbacks passed to __init__, so this
    module has no direct dependency on Flask or the global app state.
    """

    def __init__(self, wss_url, http_url, on_new_mint, on_lp_drain, lp_drain_threshold_pct=40, logger=print):
        self.wss_url = wss_url
        self.http_url = http_url
        self.on_new_mint = on_new_mint
        self.on_lp_drain = on_lp_drain
        self.lp_drain_threshold_pct = lp_drain_threshold_pct
        self.logger = logger

        self._ws = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending = {}       # request id -> purpose ("discover"/("watch_pool", key))
        self._sub_purpose = {}   # subscription id -> purpose
        self._watched_pools = {}  # key ("chain_id:address") -> pool_address
        self._connected = False
        self._stop = False

    def is_connected(self):
        return self._connected

    def start(self):
        threading.Thread(target=self._run_forever, daemon=True).start()

    def stop(self):
        self._stop = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def watch_pool(self, key, pool_address):
        """Start watching one tracked pool's transactions for a sudden
        large balance drop. Safe to call every cycle - a pool already
        being watched is skipped."""
        with self._lock:
            if key in self._watched_pools or not self._connected:
                return
            self._watched_pools[key] = pool_address
        self._send_subscribe("logsSubscribe", [{"mentions": [pool_address]}, {"commitment": "confirmed"}], ("watch_pool", key))

    def _send_subscribe(self, method, params, purpose):
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = purpose
        try:
            self._ws.send(json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        except Exception as e:
            self.logger(f"[quicknode] subscribe send failed: {e}")

    def _run_forever(self):
        if websocket is None:
            self.logger("[quicknode] websocket-client is not installed - `pip install websocket-client` to enable real-time features")
            return
        backoff = 2
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.wss_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: self.logger(f"[quicknode] ws error: {e}"),
                    on_close=lambda ws, code, msg: self._on_close(),
                )
                backoff = 2
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.logger(f"[quicknode] ws connection failed: {e}")
            self._connected = False
            if self._stop:
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _on_close(self):
        self._connected = False
        with self._lock:
            # Every subscription is gone once the socket drops - forget
            # what we'd watched so _on_open's resubscribe pass starts clean
            # (a stale watched-pool entry would otherwise silently never
            # get re-subscribed, since watch_pool() no-ops on an entry it
            # already thinks is active).
            watched = dict(self._watched_pools)
            self._watched_pools.clear()
            self._sub_purpose.clear()
            self._pending.clear()
        self._pending_resubscribe = watched

    def _on_open(self, ws):
        self._connected = True
        self.logger("[quicknode] real-time feed connected")
        for program_id in DEFAULT_LAUNCH_PROGRAM_IDS.values():
            self._send_subscribe("logsSubscribe", [{"mentions": [program_id]}, {"commitment": "confirmed"}], "discover")
        for key, pool_address in getattr(self, "_pending_resubscribe", {}).items():
            self.watch_pool(key, pool_address)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return

        if "id" in msg and "result" in msg:
            # Subscribe confirmation: msg["result"] is the subscription id.
            with self._lock:
                purpose = self._pending.pop(msg["id"], None)
                if purpose is not None:
                    self._sub_purpose[msg["result"]] = purpose
            return

        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        sub_id = params.get("subscription")
        with self._lock:
            purpose = self._sub_purpose.get(sub_id)
        if purpose is None:
            return

        value = (params.get("result") or {}).get("value") or {}
        signature = value.get("signature")
        if not signature:
            return

        if purpose == "discover":
            self._handle_discover(signature)
        elif isinstance(purpose, tuple) and purpose[0] == "watch_pool":
            self._handle_lp_check(purpose[1], signature)

    def _get_transaction(self, signature):
        return _rpc_call(
            self.http_url, "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )

    def _handle_discover(self, signature):
        # Deliberately simple heuristic: any mint whose token balance
        # appears post-transaction but didn't exist pre-transaction is
        # treated as "possibly just created." This is protocol-agnostic
        # (works the same for pump.fun and Raydium without parsing either
        # program's log format, which is fragile and changes without
        # notice) at the cost of being noisy - a false hit is just a
        # candidate that fails eligibility or scores low next cycle, the
        # same tolerance DexScreener's own discovery feeds already need.
        tx = self._get_transaction(signature)
        if not tx:
            return
        meta = tx.get("meta") or {}
        pre_mints = {b.get("mint") for b in (meta.get("preTokenBalances") or [])}
        post_mints = {b.get("mint") for b in (meta.get("postTokenBalances") or [])}
        for mint in post_mints - pre_mints:
            if mint:
                self.on_new_mint(mint)

    def _handle_lp_check(self, key, signature):
        tx = self._get_transaction(signature)
        if not tx:
            return
        meta = tx.get("meta") or {}
        pre = {b.get("accountIndex"): b for b in (meta.get("preTokenBalances") or [])}
        post = {b.get("accountIndex"): b for b in (meta.get("postTokenBalances") or [])}
        for idx, pre_bal in pre.items():
            post_bal = post.get(idx)
            try:
                pre_amt = float(pre_bal["uiTokenAmount"]["uiAmountString"])
                post_amt = float(post_bal["uiTokenAmount"]["uiAmountString"]) if post_bal else 0.0
            except (KeyError, TypeError, ValueError):
                continue
            if pre_amt <= 0:
                continue
            drop_pct = (pre_amt - post_amt) / pre_amt * 100
            if drop_pct >= self.lp_drain_threshold_pct:
                self.on_lp_drain(key, signature, drop_pct)
                return


# ---------------------------------------------------------------------------
# 4. EVM mempool early warning (mainstream chains only)
# ---------------------------------------------------------------------------

# Chains QuickNode reliably supports where subscribing to the pending-tx
# feed is worth the request volume. Everything else this tool tracks
# (the "robinhood"/"arc" chains DexScreener surfaces, for instance) is
# skipped outright rather than guessed at.
EVM_MEMPOOL_SUPPORTED_CHAINS = {"ethereum", "base", "bsc", "polygon", "arbitrum", "optimism", "avalanche"}


class EvmMempoolFeed:
    """Watches one EVM chain's pending-transaction feed for activity
    aimed at a specific set of tracked addresses (pair/router contracts),
    firing an early-warning callback seconds before a large sell or
    liquidity removal confirms on-chain.

    Cost note: `eth_subscribe("newPendingTransactions")` only gives you a
    stream of pending tx HASHES - resolving each one to see its `to`
    address costs one more RPC call. On a busy chain (Ethereum mainnet
    especially) that stream can run to thousands of hashes a minute, so
    this applies a hard rate cap (max_lookups_per_sec) and simply drops
    hashes once the cap is hit for that second, rather than queueing
    everything - this is meant to catch a heads-up on tokens already
    being tracked, not to build a complete mempool index. Expect this to
    consume meaningfully more request volume than the rest of this tool
    combined, and expect it to need a paid QuickNode plan tier for
    sustained use on a high-throughput chain.
    """

    def __init__(self, chain_id, wss_url, http_url, get_watched_addresses, on_pending_hit,
                 max_lookups_per_sec=20, logger=print):
        self.chain_id = chain_id
        self.wss_url = wss_url
        self.http_url = http_url
        self.get_watched_addresses = get_watched_addresses  # callable -> {address: key}
        self.on_pending_hit = on_pending_hit
        self.max_lookups_per_sec = max_lookups_per_sec
        self.logger = logger

        self._ws = None
        self._connected = False
        self._stop = False
        self._lookups_this_second = 0
        self._second_marker = 0

    def is_connected(self):
        return self._connected

    def start(self):
        threading.Thread(target=self._run_forever, daemon=True).start()

    def stop(self):
        self._stop = True
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run_forever(self):
        if websocket is None:
            return
        backoff = 2
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.wss_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: self.logger(f"[quicknode:{self.chain_id}] mempool ws error: {e}"),
                    on_close=lambda ws, code, msg: setattr(self, "_connected", False),
                )
                backoff = 2
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.logger(f"[quicknode:{self.chain_id}] mempool connection failed: {e}")
            self._connected = False
            if self._stop:
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _on_open(self, ws):
        self._connected = True
        self.logger(f"[quicknode:{self.chain_id}] mempool feed connected")
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newPendingTransactions"]}))

    def _rate_limited(self):
        now_sec = int(time.time())
        if now_sec != self._second_marker:
            self._second_marker = now_sec
            self._lookups_this_second = 0
        if self._lookups_this_second >= self.max_lookups_per_sec:
            return True
        self._lookups_this_second += 1
        return False

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("method") != "eth_subscription":
            return
        tx_hash = ((msg.get("params") or {}).get("result"))
        if not tx_hash or self._rate_limited():
            return

        watched = self.get_watched_addresses()
        if not watched:
            return
        tx = _rpc_call(self.http_url, "eth_getTransactionByHash", [tx_hash])
        if not tx:
            return
        to_addr = (tx.get("to") or "").lower()
        key = watched.get(to_addr)
        if key:
            self.on_pending_hit(key, tx_hash, tx)
