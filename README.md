# Dip Screener

A local web dashboard that watches [DexScreener](https://dexscreener.com) for tokens dipping hard *right now* — built to surface a flash dip as it's happening so you can buy into it, not to wait for a bounce to confirm it first. Everything runs on your own machine: a Flask server, a background polling thread, and a local config/state file. Nothing leaves your laptop except requests to DexScreener's public API, requests to your own [QuickNode](https://quicknode.com) endpoint if you've configured one (see "Real-time layer" below), and, if configured, a Telegram alert.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Then open [http://127.0.0.1:5050](http://127.0.0.1:5050) in your browser.

## How it works

Every poll cycle (90 seconds by default):

1. **Discovery** — pulls candidates from DexScreener's latest boosts, top boosts, latest token profiles, and a rotating set of search queries (not scoped to any one chain or launchpad — whatever DexScreener tracks, including Solana/pump.fun and EVM chains like Ethereum and "robinhood"), then merges in your watchlist and every token discovered on any earlier cycle (carried forward for up to 14 days so a find doesn't vanish just because it drops off a feed).
2. **Market data** — fetches price/volume/liquidity for the whole pool in batched requests (up to 30 tokens per call).
3. **Eligibility** — a deliberately loose floor (minimum liquidity, 24h volume, market cap) filters out obvious dust, not real candidates — *except* the 5-minute volume floor and live-buy-activity check, which are strict on purpose: a token can coast on a big 24h volume number for hours after trading has actually died, and that's exactly the "dead coin" case this floor exists to catch.
4. **Scoring** — every eligible token gets a 0-100 score instead of a hard pass/fail, weighted toward catching a dip as it happens rather than after it's already bounced:
   - **Short-term dip quality (20%)** — the sharpest pullback across the 15m/30m/1h windows, centered on a ~40% flash-dip sweet spot. This is the main "catch it right now" signal.
   - **Drawdown quality (25%)** — how far below its tracked all-time high, with a 20-70% sweet spot.
   - Volume retention (10%), liquidity (15%), transaction activity (10%), token age (5%).
   - **Narrative/legitimacy presence (10%)** — has a website and/or Twitter/Telegram/Discord linked. This is a cheap proxy for "real project with a community" built from data already fetched every cycle, **not** a measure of actual hype or virality (that would need a paid social-listening API this tool doesn't have) — a genuinely viral meme with no filled-in bio, or an early real project that hasn't set one up yet, can both slip through it.
   - **Buy/sell momentum (5%, deliberately small)** — a token that's actively dipping is naturally sell-heavy; that's what a dip *is*. This isn't allowed to drag the score down much just because recovery buying hasn't shown up yet.

Each token also gets a full **15m / 30m / 1h / 6h / 12h / 24h** drawdown-and-bounce breakdown (visible on hover over the "Recent dip" column), instead of a single all-time high-to-low number.

Tokens scoring at or above the alert threshold trigger a Telegram message (if configured) and appear in the Alerts feed.

## Bundling / risk check

A "Bundling" column flags scam-shaped token setups — informational only, it **never filters a token out**. A heavily flagged token can still show up and score well on its dip characteristics, but you'll see the severity right on the row instead of finding out after clicking through to a chart. Two different checks feed it depending on chain, since no single free API covers both:

- **Solana** — [RugCheck](https://rugcheck.xyz) traces how much of a token's holder base its graph analysis linked back to a single common funding wallet — the same pattern a Bubblemaps-style graph shows as one wallet spoking out to dozens of others. Severity = insider wallets ÷ total holders (not the raw count — 90 insiders out of 120 holders is a very different situation than the same 90 out of 5,000).
- **EVM chains** (Ethereum, Base, BSC, Polygon, Arbitrum, Optimism, Avalanche, and — confirmed live — the "robinhood" chain) — [GoPlus Security](https://gopluslabs.io) doesn't do wallet-cluster tracing, so this is a genuinely different, complementary signal: unlocked top-10 holder concentration (excluding burned tokens and LP/router contracts) plus contract-level red flags (honeypot, mint authority still enabled, blacklist/pause ability, extreme buy/sell tax). Any single critical flag (e.g. honeypot) forces the token straight to "Severe" regardless of concentration.

Both share the same Low/Moderate/High/Severe scale and color coding, but hovering a row's pill always says which check produced it — they are not the same metric and shouldn't be read as interchangeable.

Notes:
- A non-Solana, non-GoPlus-supported chain shows `—` (no data), never a false "clean" result.
- Checked once per token then cached for `bundling_recheck_hours` (default 6h) so it isn't re-queried every 90-second cycle — holder/contract structure doesn't shift that fast. A failed or rate-limited check doesn't get cached, though — it just retries next cycle.

## Real-time layer (QuickNode, optional)

Everything above runs on DexScreener's REST API, polled every 90 seconds — fine for scoring, but it means a brand-new token can sit unseen for a while if it hasn't yet surfaced in DexScreener's own discovery feeds. Pasting a [QuickNode](https://quicknode.com) Solana endpoint URL into Settings turns on three additional things (`quicknode_realtime.py`), all built on a plain WebSocket subscription rather than QuickNode's Streams/Webhooks product — those push to a public HTTPS URL, which a local desktop app doesn't have:

1. **Near-instant pool discovery** — watches pump.fun + Raydium program activity directly and feeds new mints into the exact same eligibility/scoring pipeline every other candidate goes through, instead of waiting for DexScreener's own discovery feeds to surface the same token. Live-tested against the real feed: raw `mentions=[pump.fun program]` activity runs to several hundred messages a second (mostly Buy/Sell/Swap, not creations), so before spending a `getTransaction` call on anything, this filters on the notification's own log lines for `Instruction: CreateV2` (confirmed live against a real token launch: `CreateV2` → `InitializeMint2` → metadata init → an immediate first buy) or `Instruction: Initialize2` (Raydium's pool-init instruction — the standard documented name, but not observed live this session, so treat it as best-effort). From there, any mint that appears in the matched transaction's token balances but wasn't there before is treated as a candidate — a noisy false positive just fails eligibility or scores low next cycle, the same tolerance DexScreener's own discovery noise already needs.
2. **Creator-wallet reputation** (Solana, via Metaplex DAS) — looks up a mint's recorded creator and counts how many other tokens that wallet has made, surfaced as a "serial creator" flag folded into the Bundling column once it crosses 15 other tokens. This is best-effort, not a verified fact: it only means anything because pump.fun sets a token's metadata creator to the real launching wallet (for its own creator-rewards feature) — a token minted another way may have no creator recorded at all, and this code has no way to independently confirm any of it. **Also: DAS is a separate add-on on QuickNode's own side** — confirmed live that an endpoint without it enabled returns a plain "Method not found," which this tool logs once and otherwise treats as "no data," not an error. Check your QuickNode dashboard if you want this piece working.
3. **Live LP-drain tripwire** — watches every transaction touching an already-tracked pool and fires an alert (Telegram + the Alerts panel) the moment one drains `quicknode_lp_tripwire_drop_pct`% (default 40%) of the pool's token balance, instead of waiting for the next bundling recheck (up to 6 hours away). Two false-positive sources were caught and fixed by testing against real transactions before this shipped, both worth knowing about since neither can be fully ruled out: a pump.fun pool "graduating" to Raydium moves ~all its reserves out in one transaction by design (suppressed by checking whether the pool's own program shows up in that transaction's logs — if so, it's the pool acting on itself, not a third party pulling funds); and `logsSubscribe(mentions=[pool])` fires on *any* transaction that merely references the pool, including as one leg of a much larger multi-hop aggregator route through unrelated pools (a real $9.71 buy routed through three pools got flagged as a 74% "drain" from an unrelated leg before this was filtered down to only the tracked token's own mint). Even fixed, this remains a heuristic — a large real trade against an already-thin pool could still cross the threshold — so treat a fired alert as "go look at the transaction" (the message links to Solscan), not an automatic verdict.

A 4th piece, **EVM mempool early-warning**, is config.json-only for now (`quicknode_evm_endpoints`, one `wss://` URL per chain — supported chains: `ethereum`, `base`, `bsc`, `polygon`, `arbitrum`, `optimism`, `avalanche`; anything else this tool tracks, like the "robinhood"/"arc" chains, isn't a chain QuickNode supports and is skipped outright). It flags a pending transaction sent *directly* to a tracked pool/pair address before it confirms — genuinely useful lead time, but narrower than it sounds: most real sells on an EVM chain go through a router contract with the token address buried in calldata, which this does **not** decode, so it only catches direct-to-pool activity, not the common router-mediated swap. Also: `eth_subscribe("newPendingTransactions")` only gives you transaction hashes, and resolving each one costs a follow-up RPC call — on a busy chain like Ethereum mainnet that's a lot of request volume, so this applies a hard per-second rate cap and simply drops hashes once it's hit, rather than queueing everything. Expect this to need a paid QuickNode plan tier for sustained use.

All four are independent and individually toggleable; every one of them is a no-op with nothing configured, and a feed that can't connect (bad URL, endpoint down) retries with backoff and logs the error — it never takes down the poll cycle. The "Real-time layer: ..." line under the QuickNode field in Settings shows live connection status per feed.

**A real limit to know about**: the pump.fun program-mentions subscription is genuinely high-volume (hundreds of messages/second live-measured), and discovery + the LP tripwire both spend a `getTransaction` RPC call per match. On a free/lower QuickNode plan this can burn through your rate limit fast — confirmed live as repeated WebSocket disconnects (`code 1001, "upstream went away"`) and an `HTTP 429 Too Many Requests` on a DAS call, both from normal operation plus testing traffic in a short window, not a bug in the subscription logic itself. If you see the "Real-time layer" status flapping between connected/reconnecting, check your QuickNode dashboard for request-volume/rate-limit info on this endpoint before assuming something's broken.

## Built for small, careful portfolios

This isn't tuned to maximize the number of tokens shown — it's tuned to help you not gamble away a small amount of money:

- The **Narrative** column (see Scoring above) nudges the ranking toward tokens that look like real projects rather than anonymous copy-paste deploys.
- The **Bundling** column surfaces scam-shaped red flags right on the row instead of making you dig for them.
- A one-click **Conservative preset** in the filter bar (Tracked tokens panel) raises the bar to $50K+ liquidity and $100K+ market cap and hides anything flagged Moderate risk or worse — a fast way to cut the list down to the tokens most worth a beginner's attention. There's also a standalone **"Hide Moderate+ bundling risk"** checkbox if you just want that part without the size filters.
- None of this is a hard eligibility gate — a token can still be discovered and scored even if it wouldn't pass the Conservative preset. The filters are a *view*, so you can always widen back out; they never limit what's actually being tracked underneath.

## Paper trading

A manual-only buy-the-dip trainer, for practicing entries and exits without risking real money:

- Click the **$** button on any tracked token row to open a simulated position at the current price, for whatever dollar amount you choose.
- Click **Sell** on an open position in the Paper Trading panel to close it at whatever the current price is then — nothing auto-buys or auto-sells, so both the entry and the exit are your call.
- Starts with $10,000 in fake cash; tracks open positions (unrealized P&L), closed trade history, and an overall win rate.
- **Reset portfolio** clears everything back to the starting balance (two clicks required, to avoid an accidental wipe).

## Configuration

Settings live in `config.json` (copy `config.example.json` to get started — `config.json` and `state.json` are gitignored since they hold your local runtime data and, potentially, a Telegram bot token). Most of it is also editable from the Settings panel in the UI:

| Setting | What it does |
|---|---|
| `auto_discover` | Turn automatic token discovery on/off |
| `poll_interval_seconds` | How often to poll (default 90s) |
| `alert_score_threshold` | Minimum score to trigger an alert |
| `eligibility_min_*` / `eligibility_max_*` | Loose floor/ceiling applied before scoring |
| `eligibility_min_volume_5m_usd` | Minimum 5-minute dollar volume to be scored at all (default $5,000) — the main dead-coin filter, paired with a live-buy-activity check |
| `known_universe_retention_days` | How long a discovered token stays tracked after it last appears anywhere |
| `bundling_check_enabled` / `bundling_recheck_hours` | Toggle the RugCheck bundling check and how often it's refreshed per token (default on, 6h) |
| `telegram_bot_token` / `telegram_chat_id` | Optional Telegram alerts |
| `quicknode_solana_wss_url` | QuickNode Solana endpoint (also settable from Settings in the UI) — see "Real-time layer" |
| `quicknode_realtime_discovery_enabled` / `quicknode_das_enabled` / `quicknode_lp_tripwire_enabled` | Toggle each real-time piece independently (all default on once the endpoint above is set) |
| `quicknode_lp_tripwire_drop_pct` | % of a pool's token balance lost in one transaction that counts as a drain (default 40) |
| `quicknode_evm_endpoints` / `quicknode_mempool_enabled` | `{"ethereum": "wss://...", ...}` — EVM mempool early-warning, config.json-only, mainstream chains only |

## Project structure

```
app.py                 Flask app, background polling loop, HTTP API
screener_core.py       Discovery, scoring, and drawdown/window calculations
quicknode_realtime.py  Optional real-time layer (QuickNode WebSocket feeds + Metaplex DAS)
templates/index.html   Dashboard page
static/app.js          Dashboard UI logic
static/style.css       Dashboard styling
config.json             Your local config (gitignored)
state.json               Persisted price history / known universe (gitignored)
```
