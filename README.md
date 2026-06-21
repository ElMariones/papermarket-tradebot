# TradeBOT — Polymarket Paper-Trading Agent + Dashboard

An autonomous **paper-trading** agent for [Polymarket](https://polymarket.com)
prediction markets, paired with a real-time trading-terminal dashboard.

It pulls **real live market data** (CLOB order books + Gamma market metadata),
runs a strategy, and **simulates** every fill against the real order book —
walking actual bids/asks for a true average price and slippage.
**No wallet, no keys, no real money — ever.** Execution is always simulated.

```
 ┌──────────────┐   live prices    ┌──────────────────┐  simulated fills   ┌────────────┐
 │ Polymarket   │ ───────────────▶ │  Agent loop       │ ─────────────────▶ │  SQLite    │
 │ CLOB + Gamma │                  │  strategy + risk  │                    │  ledger    │
 └──────────────┘                  └──────────────────┘                    └─────┬──────┘
                                                                                  │
                                                       ┌──────────────────────────▼─────┐
                                                       │  Dashboard (web terminal UI)    │
                                                       └─────────────────────────────────┘
```

---

## Quick start

```bash
git clone https://github.com/ElMariones/papermarket-tradebot.git
cd papermarket-tradebot
./run.sh
# then open http://127.0.0.1:8765
```

That single command starts the dashboard **and** the agent worker (in-process).
Click **Start** in the top bar to begin trading, **Cycle** to run one scan
immediately, or **Pause / Stop** to halt.

**Requirements:** Python 3.11+. The only third-party dependency is `certifi`
(`run.sh` installs it for you).

---

## Profiles — four independent bots

The dashboard runs **four separate bots** — **Kaladin**, **Adolin**, **Dalinar**,
and **Renarin** — selectable from the top bar. Each is a fully independent
portfolio: its own money, strategy parameters, open positions, trade history,
reasoning log, equity curve, exports, and start/pause/stop state. They share
nothing, run their own agent loops in parallel, and **resetting one wipes only
that bot.** Each starts from a distinct strategy preset (balanced, aggressive,
conservative, nimble) that you can edit live per profile.

## How it works

Each agent cycle does three things:

1. **Manage open positions** — settle markets that have resolved, and apply exit
   rules to the rest.
2. **Scan live markets** — pull the top markets by 24h volume, score each one
   through the strategy, and place simulated buys that clear both the strategy's
   confidence bar and the portfolio's risk guards.
3. **Snapshot equity** — record a point on the equity curve.

Defaults: `$200` starting paper balance, up to 5% of equity risked per trade
(scaled by confidence), capped at a configurable number of concurrent positions.
Everything persists to SQLite and survives restarts.

### The strategy — favorite-longshot bias

Prediction and betting markets show a durable inefficiency: **longshots are
systematically overpriced** and **clear favorites are slightly underpriced.**
TradeBOT harvests it with one symmetric rule — **buy the favorite side** (YES or
NO, whichever is priced higher) when its price sits inside the underpriced band
(default `0.80–0.96`). Buying the favorite *is* fading the overpriced longshot on
the other side.

Every candidate must clear three liquidity gates before any trade:

1. 24-hour volume ≥ `min_volume24h`
2. resting order-book depth ≥ `min_book_usd` on the side it would hit
3. bid/ask spread ≤ `max_spread`

…and it never pays above `max_entry_price` (default `0.97`, so there's real
upside left). Position size scales with **upside room** — the cheaper a favorite
is inside the band, the more convergence room it has to resolution, so it earns a
larger (still risk-capped) allocation.

### Exits — built around resolution

The favorite-longshot edge is realized when a market **resolves** and the
favorite converges toward its true value, so exits are tuned to let winners run
to settlement rather than scalp intraday noise:

- **Resolution settlement** — when a market resolves, the position is booked at
  the real `0` / `1` outcome. This is detected robustly even though the live
  order book disappears the moment a market resolves.
- **Take-profit** recycles capital once a position has run far enough.
- **Stop-loss** combines a wide percentage stop with an absolute price floor, so
  a position is cut only when its thesis is genuinely broken (the "favorite" is
  no longer favored) — not on ordinary price wobble.
- **Re-entry cooldown** prevents the bot from immediately re-buying a market it
  just exited.

Every decision — acted on **or** passed — is logged in plain English and shown
in the dashboard's **Reasoning Log** tab.

> A deep dive into the strategy, a worked analysis of a real trading run, and the
> reasoning behind these exit rules lives in
> [`TRADING_ANALYSIS.md`](./TRADING_ANALYSIS.md).

---

## Dashboard

- **Headline tape:** total value, total P&L (realized + unrealized), cash, win
  rate, open/max positions, drawdown.
- **Equity curve** — a hand-rolled canvas chart.
- **Open positions** — live mark-to-market with one-click close.
- **Trade history**, **Reasoning Log**, and **Hourly Log** tabs.
- **Add paper funds** — deposits raise the cost basis so P&L stays honest.
- **Start / Pause / Stop / Cycle** agent controls.
- **Strategy parameter editor** — every knob below, no code editing.
- **Light / dark theme** toggle (follows your OS preference, remembers your choice).
- **Export** — download the full history as JSON, or the visible tab as CSV.

All timestamps render in your **local timezone**; stored values are UTC.

---

## Tech

- **Backend:** Python 3.11, standard library only (`http.server`) — a
  zero-framework REST API that also hosts the static dashboard. `certifi` is the
  lone dependency, used to verify HTTPS to Polymarket.
- **Frontend:** vanilla HTML / CSS / JavaScript — no build step, no framework.
- **Storage:** a single SQLite file (WAL mode) holds the entire ledger.
- **Market data:** Polymarket's CLOB API (order books, prices) and Gamma API
  (market discovery / metadata), both public and read-only.

### Project layout

```
.
├── backend/
│   ├── engine.py             # ledger + extensions (funds, settings, equity, decisions)
│   ├── paper_engine_core.py  # fill simulation + SQLite core (real order-book walking)
│   ├── polymarket_client.py  # live market discovery (Gamma API)
│   ├── strategy.py           # favorite-longshot decision engine
│   ├── agent.py              # cycle: manage exits → scan → trade; run-forever loop
│   ├── worker.py             # standalone agent process entrypoint
│   └── server.py             # stdlib REST API + static dashboard host
├── frontend/                 # index.html · styles.css · app.js (vanilla)
├── Dockerfile · requirements.txt · .env.example · run.sh
```

---

## Configuration

| What | How |
|------|-----|
| Starting balance | `TRADEBOT_START_BALANCE=500 ./run.sh` (used only when the DB is first created) |
| Add funds later | Dashboard → **Add Paper Funds**, or `POST /api/add-funds {"amount":100}` |
| Strategy params | Dashboard → **Strategy Parameters** → Save (persisted in the DB) |
| Max positions | Dashboard → **Position Capacity** (raises both the strategy cap and the portfolio risk cap) |
| Scan interval | `scan_interval_sec` param (default 60s) |
| DB location | `TRADEBOT_DB_PATH` env (default `~/.polymarket-paper/portfolio.db`) |
| Port | `PORT` env (default 8765) |
| Optional login | Set `TRADEBOT_AUTH_PASSWORD` (and optional `TRADEBOT_AUTH_USER`) to require HTTP Basic auth for the whole site. Unset = open. |

### CLI (no dashboard)

```bash
cd backend
python3 polymarket_client.py              # pull 5 live markets + prices
python3 agent.py --cycles 3 --interval 5  # run 3 agent cycles against live data
python3 worker.py                         # run the agent loop forever
```

### Reset

Dashboard → **Danger Zone → Reset Everything** (asks for confirmation). Wipes all
positions, trades, equity history, decisions, and reports, then restarts the
balance at the amount you enter. Strategy parameters are kept. API equivalent:
`POST /api/reset {"confirm":true,"balance":200}`.

---

## Safety: this is paper trading

`backend/agent.py` carries `LIVE_TRADING = False`, and there is **no
live-execution code path in this project at all** — flipping that flag does
nothing on its own. A future live build would have to deliberately add an
order-submission path. Market data is real; execution is always simulated.

---

## License

Released as an educational reference for prediction-market mechanics and
order-book fill simulation. Not financial advice.
