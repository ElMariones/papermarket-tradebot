# TradeBOT — Polymarket Paper-Trading Agent + Dashboard

An autonomous **paper-trading** agent for [Polymarket](https://polymarket.com)
prediction markets, with a local (or cloud-hosted) trading-terminal dashboard.

It pulls **real live market data** (CLOB order books + Gamma market metadata),
applies a strategy, and **simulates** every fill against the real order book.
**No wallet, no keys, no real money — ever.** All execution is simulated.

```
 ┌──────────────┐   live prices    ┌─────────────────┐   simulated fills   ┌────────────┐
 │ Polymarket   │ ───────────────▶ │  Agent loop      │ ─────────────────▶ │  SQLite    │
 │ CLOB + Gamma │                  │  strategy + risk │                     │  ledger    │
 └──────────────┘                  └─────────────────┘                     └─────┬──────┘
                                                                                  │
                                                       ┌──────────────────────────▼─────┐
                                                       │  Dashboard (dark terminal UI)   │
                                                       └─────────────────────────────────┘
```

---

## Quick start (local)

```bash
cd /Volumes/SanDisk2TB/TradeBOT
./run.sh
# then open http://127.0.0.1:8765
```

That single command starts the dashboard **and** the agent worker (in-process).
Click **Start** in the top bar to begin trading, **Cycle ▸** to run one scan
immediately, or **Pause/Stop** to halt.

Requirements: Python 3.11+. The only third-party dependency is `certifi`
(`pip install -r requirements.txt`, which `run.sh` does for you).

---

## What it does

- **Starting paper balance:** `$200` (configurable, see below).
- **Risk per trade:** max 5% of total value, scaled by confidence.
- **Max concurrent positions:** 10.
- **Strategy:** favorite-longshot bias exploitation (see below).
- **Persistence:** everything is in SQLite and survives restarts.

### The strategy — favorite-longshot bias

Prediction (and betting) markets show a durable inefficiency: **longshots are
systematically overpriced** and **clear favorites are slightly underpriced.**
TradeBOT harvests both sides:

- **Buy YES on favorites** priced in the `[fav_low, fav_high]` band (default
  `0.80–0.96`) — the favorite is underpriced.
- **Fade longshots by buying NO** when the YES price is below `longshot_thresh`
  (default `0.06`) — the longshot is overpriced, so NO carries positive EV.

Every candidate must clear three liquidity gates before any trade:
1. 24-hour volume ≥ `min_volume24h`
2. resting order-book depth ≥ `min_book_usd` on the side we'd hit
3. bid/ask spread ≤ `max_spread`

…and never pays above `max_entry_price` (default `0.97`, so there's real upside
left). Position size = `risk_per_trade_pct × total_value × confidence`, capped by
the 5% per-trade risk limit and available cash.

**Exits:** take-profit (`+take_profit_pct` vs entry), stop-loss
(`−stop_loss_pct`), or **market resolution** (settled at the real 0/1 outcome).

Every decision — acted on **or** passed — is logged in plain English and visible
in the dashboard's **Reasoning Log** tab.

### Fills are realistic

Market orders are **walked through the real order book** (consuming asks for
buys, bids for sells) to compute a true average fill price and slippage — not a
naive mid-price. See `backend/paper_engine_core.py::_simulate_fill`.

---

## Dashboard

- **Headline:** total value, total P&L (realized + unrealized), cash, win rate,
  open/max positions, drawdown.
- **Equity curve** (hand-rolled canvas chart).
- **Open positions** with live mark-to-market and one-click close.
- **Trade history** and **Reasoning Log** tabs.
- **Add paper funds** (deposits raise the cost basis so P&L stays honest).
- **Start / Pause / Stop / Cycle** agent controls.
- **Strategy parameter editor** — every knob above, no code editing.

---

## Configuration

| What | How |
|------|-----|
| Starting balance | `TRADEBOT_START_BALANCE=500 ./run.sh` (only used on first DB creation) |
| Add funds later | Dashboard → **Add Paper Funds**, or `POST /api/add-funds {"amount":100}` |
| Strategy params | Dashboard → **Strategy Parameters** → Save (persisted in DB) |
| Scan interval | `scan_interval_sec` param (default 60s) |
| DB location | `TRADEBOT_DB_PATH` env (default `~/.polymarket-paper/portfolio.db`) |
| Port | `PORT` env (default 8765 local) |

### CLI (no dashboard)

```bash
cd backend
python3 polymarket_client.py            # pull 5 live markets + prices
python3 agent.py --cycles 3 --interval 5  # run 3 agent cycles against live data
python3 worker.py                       # run the agent loop forever
```

### Where the data lives

A single SQLite file (default `~/.polymarket-paper/portfolio.db`) holds
portfolios, positions, trades, daily snapshots, intraday equity snapshots, the
decision/reasoning log, agent settings, and agent control state.

---

## Project layout

```
TradeBOT/
├── backend/
│   ├── engine.py             # ledger + extensions (funds, settings, equity, decisions)
│   ├── paper_engine_core.py  # vendored fill-sim + SQLite core (real order-book walking)
│   ├── polymarket_client.py  # live market discovery (Gamma API)
│   ├── strategy.py           # favorite-longshot decision engine
│   ├── agent.py              # cycle: manage exits → scan → trade; run_forever loop
│   ├── worker.py             # standalone agent process entrypoint
│   └── server.py             # stdlib REST API + static dashboard host
├── frontend/                 # index.html · styles.css · app.js (vanilla, dark terminal)
├── Dockerfile · fly.toml · requirements.txt · .env.example · run.sh
```

---

## Safety: this is paper trading

`backend/agent.py` carries `LIVE_TRADING = False`. There is **no live-execution
code path in this project at all** — flipping that flag does nothing on its own;
a future live build would have to deliberately add an order-submission path. This
boundary is identical locally and when deployed. Market data is real; execution
is always simulated.

---

## Deployment (Fly.io) — always-on in the cloud

> **Architecture decision (important):** A Fly **Volume attaches to one machine
> at a time**, and TradeBOT stores state in a single SQLite file. So we run the
> web server **and** the agent worker **in one machine** (the web process runs
> the worker loop in-process via `TRADEBOT_STANDALONE=1`). Splitting into
> separate `web` and `worker` machines — as a `[processes]` block would — means
> two machines can't share the SQLite volume; that path requires switching to
> **Fly Postgres**. For a one-person paper bot, **one machine + SQLite on a
> volume** is simpler, cheaper, and correct. The worker never idles out because
> `auto_stop_machines = false` and `min_machines_running = 1` keep the machine
> always on (it has no inbound HTTP of its own).

### One-time setup

```bash
# 0. Install tooling if needed:
#    GitHub:  brew install gh   (then: gh auth login)
#    Fly:     curl -L https://fly.io/install.sh | sh   (then: fly auth login)

# 1. Push to GitHub (private repo named after the project):
gh repo create tradebot-polymarket --private --source=. --remote=origin --push

# 2. Create the Fly app + volume (edit `app` name in fly.toml first — names are global):
fly apps create tradebot-polymarket
fly volumes create tradebot_data --size 1 --region iad   # match primary_region

# 3. Deploy:
fly deploy

# 4. Open it:
fly open          # public https URL, e.g. https://tradebot-polymarket.fly.dev
```

There are **no secrets** to set (paper trading needs none). If you later add any,
use `fly secrets set KEY=value` — never put secrets in `fly.toml` or git.

### Verify the deploy

```bash
fly status                       # machine is "started"
fly logs                         # watch for "Agent worker running in-process" + cycle activity
curl -s https://<your-app>.fly.dev/api/agent   # {"status":"running","cycles":N,...}
```

**Data-persistence test (the one that matters):**
```bash
fly machine restart <machine-id>   # or: fly deploy --strategy immediate
# then reload the dashboard — your balance, positions and trade history are intact
# because the DB is on the /data volume, not ephemeral container storage.
```

### Day-to-day

| Task | Command / action |
|------|------------------|
| Deploy a change | `git commit -am "..." && git push && fly deploy` |
| Check logs | `fly logs` |
| Is the agent alive? | `curl https://<app>.fly.dev/api/agent` → `status:running`, rising `cycles` |
| Add funds / change params | Just use the **dashboard UI** — no SSH, no redeploy |
| Check from your phone | Open the `https://<app>.fly.dev` URL in any mobile browser |

> Optional: add a GitHub Action to auto-deploy on push to `main`
> (`superfly/flyctl-actions`), using a `FLY_API_TOKEN` repo secret from
> `fly tokens create deploy`. The manual `fly deploy` flow above is fine to start.

---

## Status / rough edges (v2 ideas)

- Strategy is intentionally simple (one explainable edge). Add momentum/news
  signals as additional, separately-toggleable strategies.
- Equity curve is intraday snapshots; add selectable time ranges.
- No backtest harness yet — it trades forward only.
- Single portfolio in the UI (the engine supports more by name).
