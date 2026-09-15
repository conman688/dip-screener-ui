# Dip Screener

A local web dashboard that watches [DexScreener](https://dexscreener.com) for tokens that dipped hard and are showing signs of recovery — the "next CATE-type setup" scanner. Everything runs on your own machine: a Flask server, a background polling thread, and a local config/state file. Nothing leaves your laptop except requests to DexScreener's public API and, if configured, a Telegram alert.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

Then open [http://127.0.0.1:5050](http://127.0.0.1:5050) in your browser.

## How it works

Every poll cycle (90 seconds by default):

1. **Discovery** — pulls candidates from DexScreener's latest boosts, top boosts, latest token profiles, and a rotating set of search queries, then merges in your watchlist and every token discovered on any earlier cycle (carried forward for up to 14 days so a find doesn't vanish just because it drops off a feed).
2. **Market data** — fetches price/volume/liquidity for the whole pool in batched requests (up to 30 tokens per call).
3. **Eligibility** — a deliberately loose floor (minimum liquidity/volume/market cap) filters out obvious dust, not real candidates.
4. **Scoring** — every eligible token gets a 0-100 score instead of a hard pass/fail, weighted across:
   - Drawdown quality (how far below its tracked all-time high, with a 20-70% "sweet spot")
   - Short-term dip quality (the sharpest pullback across the 15m/30m/1h windows — catches a token that's up big over 24h but just dropped hard in the last hour)
   - Volume retention, liquidity, buy/sell momentum, transaction activity, token age

Each token also gets a full **15m / 30m / 1h / 6h / 12h / 24h** drawdown-and-bounce breakdown (visible on hover over the "Recent dip" column), instead of a single all-time high-to-low number.

Tokens scoring at or above the alert threshold trigger a Telegram message (if configured) and appear in the Alerts feed.

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
| `known_universe_retention_days` | How long a discovered token stays tracked after it last appears anywhere |
| `telegram_bot_token` / `telegram_chat_id` | Optional Telegram alerts |

## Project structure

```
app.py              Flask app, background polling loop, HTTP API
screener_core.py     Discovery, scoring, and drawdown/window calculations
templates/index.html Dashboard page
static/app.js        Dashboard UI logic
static/style.css     Dashboard styling
config.json           Your local config (gitignored)
state.json             Persisted price history / known universe (gitignored)
```
