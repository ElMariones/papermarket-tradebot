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
 └──────┬───────┘                  └──────────────────┘                    └─────┬──────┘
        │ live order books                                                        │
        │                          ┌──────────────────┐   same fill engine       │
        └────────────────────────▶ │  Manual trading   │ ────────────────────────▶│
                                   │  (Markets page)   │                          │
                                   └────────▲─────────┘                          │
                                            │ owner-only writes                  │
                              ┌─────────────┴──────────────┐   reads open to all │
                              │  Auth: accounts + sessions  │◀────────────────────┘
                              │  admin / user / spectator   │
                              └─────────────▲──────────────┘
                                            │
                               ┌────────────┴───────────────┐
                               │  Dashboard (web terminal)   │
                               └────────────────────────────┘
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

## Profiles — four bots per account

There are four bot identities — **Kaladin**, **Adolin**, **Dalinar**, and
**Renarin** — each with a distinct strategy preset (balanced, aggressive,
conservative, nimble). **Every account owns its own independent copy of all
four.** A copy is a full portfolio: its own money, editable strategy
parameters, open positions, trade history, reasoning log, equity curve,
exports, agent loop and start/pause/stop state. Sara can turn *her* Dalinar
into an aggressive buyer running on $50 while Mario's Dalinar stays
conservative — the copies share nothing, and **resetting one wipes only that
copy.** Four accounts means sixteen bots running in parallel (they share one
cached market scan, so the network load doesn't multiply).

The top bar has a two-level switcher: pick an account, then one of their
four bots. Spectators can browse every account's set.

## Accounts & roles

The site is a public trading terminal with named accounts on top. There is
**no signup form** — the admin creates every account from the CLI:

```bash
python3 backend/create_user.py <username> <password>          # regular user
python3 backend/create_user.py <username> <password> --role admin
```

One command creates the account **and** its set of four bots (default `$200`
each, or `--balance N` per bot), created **stopped** — nothing trades until
the owner presses Start. Run it wherever the server's DB lives (locally, or
`fly ssh console` on a deploy). The **first admin** account doesn't get fresh
copies: it **claims the original demo set, history and all** (see the
migration note below).

| Role | Can view | Can control |
|------|----------|-------------|
| **Spectator** (no login) | everything — every account's bots, P&L included | nothing; every control is visible but locked, and clicking one points to requesting an account |
| **User** | everything | their **own four bots** only: start/pause/stop/cycle, strategy params, manual buy/sell, funds, reset |
| **Admin** | everything | everything — every account's bots |

Visibility is deliberately unrestricted — the whole point is that friends can
watch every account's bots compete. The restriction is on *actions*, not
viewing. Sessions are opaque DB-backed tokens in an `httpOnly` cookie
(30 days, revocable by deleting the row); passwords are `pbkdf2_hmac` —
still zero third-party dependencies.

> **Migration note:** existing single-password deployments keep working — if
> `TRADEBOT_AUTH_PASSWORD` is set and **no accounts exist yet**, the whole
> site stays behind HTTP Basic Auth exactly as before (and whoever passes it
> has full control, as before). A fresh install with no accounts runs an
> unowned demo set of the four bots. When the first **admin** account is
> created, it claims that demo set — portfolios, trade history, logs and
> equity curves are renamed to `admin:Bot` and kept — and the Basic Auth
> gate retires (the env var is then ignored; you can unset it).

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
- **Trade history**, **Reasoning Log**, **Hourly Log**, and **Ask the Bot**
  (local AI chat over the log — see below) tabs.
- **Add paper funds** — deposits raise the cost basis so P&L stays honest.
- **Start / Pause / Stop / Cycle** agent controls.
- **Strategy parameter editor** — every knob below, no code editing.
- **Light / dark theme** toggle (follows your OS preference, remembers your choice).
- **Export** — download the full history as JSON, or the visible tab as CSV.

All timestamps render in your **local timezone**; stored values are UTC.

## Ask the Bot (local AI, Apple Silicon only)

A chat tab on the dashboard that answers natural-language questions about the
selected bot's own trading — *"why did you buy NO on Spain winning the World
Cup?"*, *"what positions did you open today?"* — generated by a small LLM
running **fully on-device via Apple's [MLX](https://github.com/ml-explore/mlx)
framework**. No API calls, no cloud, no keys.

Answers are **grounded, not free-associated**: each question retrieves the
most relevant entries from that bot's Reasoning Log / Trade History (embedded
incrementally into a `rag_chunks` table in the same SQLite file, cosine
similarity over in-memory numpy — no vector database at this corpus size),
and the model is instructed to answer *only* from those entries, saying "I
don't see anything in the log about that" rather than guessing. Every answer
shows the exact log entries it cites, expandable under the reply — the model
isn't just answering, it's citing its own logs. A bot with an empty log skips
the model entirely. The chat is **read-only** and open to spectators: it
cannot start, stop, or trade anything, regardless of how a question is phrased.

**Requirements:** an Apple Silicon Mac and two optional dependencies —

```bash
python3 -m venv ~/.polymarket-paper/venv
~/.polymarket-paper/venv/bin/pip install mlx-lm sentence-transformers 'transformers<5'
./run.sh   # picks up the venv automatically when it exists
```

(The `transformers<5` pin matters: current `mlx-lm` breaks against
transformers 5.13+. Keep the venv on your internal disk — `transformers`
cannot load from an exFAT volume with macOS `._*` metadata files.)

The hosted **Fly.io deployment does not run this tab, by design** — MLX is
Apple-Silicon-only and the whole point is local, offline inference; a cloud
"fallback" would defeat it. On any machine without the stack the dashboard
runs exactly as before and the tab explains itself instead of erroring.
Availability is probed once at boot (`backend/rag/__init__.py`), never at
request time.

Default model: `mlx-community/Qwen2.5-3B-Instruct-4bit` (~1.8 GB, downloaded
from Hugging Face on first question, then cached). Swap via
`TRADEBOT_LLM_MODEL` (any mlx-community instruct model); embeddings default
to `all-MiniLM-L6-v2`, swappable via `TRADEBOT_EMBED_MODEL`. The model loads
once per server process, on the first question — the UI shows a "waking up
the local model…" state for that one slow answer.

## Manual trading — the Markets page

`/markets` shows the **same live scan the bots trade from** (top Polymarket
markets by 24h volume, via the Gamma API) — deliberately **unfiltered**, so a
human can trade markets the bots skip. Each row shows live YES/NO prices and
24h volume, flags which markets would pass the bots' baseline volume +
price-band gates (as a reference, not a restriction), tags the market classes
some bots exclude (`single match`, `inplay`), and has a **book** button that
pulls the live order book on demand (best bid/ask, spread, walkable depth).

There is **no separate manual portfolio**: a manual trade lands in **one of
your bots' portfolios**, alongside that bot's own automatic trades. The
target defaults to whichever bot is selected in the terminal top bar (a
selector on the page switches between your copies). Signed-in users get
**Buy YES / Buy NO** with an amount, and one-click **sell** on the target
bot's open positions. Execution goes through the **exact same order-book
fill simulation the agent uses** (`paper_engine_core.py` walks the real CLOB
book for true average price and slippage) — there is no second, simplified
pricing path to drift from reality. The only house rules: you can't spend
more cash than the bot holds, and an empty book can't be filled. The bots'
confidence bars and liquidity gates don't apply to humans.

Manual fills are tagged `source: "manual"` (vs `"agent"`) in the ledger and
shown with a ✋ badge in trade history, so your picks and the bot's are
always distinguishable — including in CSV/JSON exports. Spectators see the
full Markets page too; the trade buttons are locked.

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
│   ├── auth.py               # accounts + sessions (pbkdf2, opaque cookie tokens)
│   ├── create_user.py        # admin CLI: create account + its four bots
│   ├── rag/                  # "Ask the Bot" — local MLX RAG over the log
│   │   ├── llm.py            #   mlx-lm singleton: load once, generate
│   │   ├── embed.py          #   chunk + embed log entries (MiniLM)
│   │   ├── index.py          #   incremental vector index in SQLite + search
│   │   └── ask.py            #   retrieval + grounded prompt + answer
│   └── server.py             # stdlib REST API + static host + permission checks
├── frontend/
│   ├── index.html · app.js   # the trading terminal
│   ├── markets.html · markets.js   # live market browser + manual buy/sell
│   ├── login.html            # sign-in (no registration — accounts via CLI)
│   ├── auth-ui.js            # shared auth state + spectator modal
│   └── styles.css
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
| Accounts | `python3 backend/create_user.py <user> <pass> [--role admin] [--balance N]` — see **Accounts & roles**. Session lifetime is the `SESSION_TTL_DAYS` constant in `backend/auth.py` (30 days); no secret env var needed — tokens are random and DB-backed. |
| Ask the Bot model | `TRADEBOT_LLM_MODEL` (default `mlx-community/Qwen2.5-3B-Instruct-4bit`), `TRADEBOT_EMBED_MODEL` (default `all-MiniLM-L6-v2`), `TRADEBOT_LLM_MAX_TOKENS` (default 400). Apple Silicon only — see **Ask the Bot**. |
| Server python | `TRADEBOT_PYTHON` — interpreter `run.sh` uses; defaults to `~/.polymarket-paper/venv/bin/python3` when that venv exists (enables Ask the Bot), else `python3`. |
| Legacy login (deprecated) | `TRADEBOT_AUTH_PASSWORD` (+ optional `TRADEBOT_AUTH_USER`) still gates the whole site with HTTP Basic Auth, but **only while no accounts exist**. Ignored once the first account is created. |

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
