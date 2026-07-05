# TradeBOT — Autonomous Paper-Trading Platform for Prediction Markets

**Python (stdlib) · Vanilla JS · SQLite · Real market data · On-device LLM (Apple MLX)**

TradeBOT is a full-stack trading platform for [Polymarket](https://polymarket.com)
prediction markets. Autonomous bots scan live markets, trade a quantified
strategy against **real order books**, and stream their results to a
multi-user trading-terminal dashboard — where every decision the bots make
is logged in plain English and can be interrogated through a **fully local,
retrieval-grounded AI chat**.

All market data is real and live. All execution is **simulated** — every
fill is computed by walking the actual order book, but no wallet, no keys,
and no real money are ever involved.

---

## What it demonstrates

- **Systems design with deliberate constraints** — the entire backend is
  Python standard library (`http.server`, `sqlite3`, `urllib`): a REST API,
  session-based auth, a multi-threaded agent runtime and a static file host
  with **one** third-party dependency (`certifi`). No framework, no ORM, no
  build step.
- **Quantitative strategy implementation** — a documented market
  inefficiency (favorite-longshot bias) turned into code, with liquidity
  gates, confidence-weighted position sizing, risk caps, and exit rules
  designed around how prediction markets actually settle.
- **Market microstructure realism** — simulated fills walk real CLOB bid/ask
  levels for true average price and slippage; one shared fill engine serves
  both the bots and human traders so results never diverge between paths.
- **Multi-tenant product thinking** — accounts, roles, per-user bot fleets,
  and a spectator mode designed so outsiders can watch everything but touch
  nothing.
- **Applied local AI** — a RAG pipeline (embeddings → incremental vector
  index → similarity + recency retrieval → grounded generation) running
  entirely on-device via Apple MLX, engineered against hallucination.

---

## Architecture

```
 ┌──────────────┐   live prices     ┌───────────────────┐  simulated fills  ┌────────────┐
 │  Polymarket  │ ────────────────▶ │    Agent loops     │ ────────────────▶ │   SQLite   │
 │ CLOB + Gamma │   (shared scan    │  one per bot ×     │                   │   ledger   │
 │  (read-only) │      cache)       │  per account       │                   │ (WAL mode) │
 └──────┬───────┘                   └───────────────────┘                   └─────┬──────┘
        │ live order books                                                        │
        │                           ┌───────────────────┐   same fill engine     │
        └─────────────────────────▶ │  Manual trading    │ ──────────────────────▶│
                                    │  (Markets page)    │                        │
                                    └─────────▲─────────┘                        │
                                              │ owner-only writes               │
                                ┌─────────────┴──────────────┐  reads open      │
                                │  Auth: sessions + roles     │◀─────────────────┤
                                │  admin / user / spectator   │                  │
                                └─────────────▲──────────────┘                  │
                                              │                        ┌────────▼─────────┐
                                ┌─────────────┴──────────────┐         │  "Ask the Bot"   │
                                │   Dashboard (web terminal)  │◀───────│  local MLX RAG   │
                                └────────────────────────────┘         └──────────────────┘
```

**Backend** (`backend/`) — a threaded stdlib HTTP server exposing a JSON API
and hosting the frontend. Agent loops run in-process as supervised daemon
threads (one per bot portfolio, created dynamically as accounts are added).
All control state (running / paused / stopped) lives in the database, so the
web process and any standalone worker process coordinate through SQLite
alone — no message broker needed at this scale.

**Frontend** (`frontend/`) — hand-written HTML/CSS/JS. A trading-terminal UI
with a canvas-rendered equity curve, live polling, light/dark theming, and
optimistic view management (see *Engineering decisions*).

**Storage** — a single SQLite file in WAL mode holds portfolios, positions,
trades, strategy settings, equity history, the plain-English decision log,
user accounts, sessions, and the AI chat's vector index.

---

## The trading system

### Strategy: favorite-longshot bias

Prediction markets exhibit a well-documented inefficiency: **longshots are
systematically overpriced and clear favorites slightly underpriced.**
TradeBOT harvests it with one symmetric rule — buy whichever side (YES/NO)
is the favorite when its price sits inside a configurable "underpriced band"
(default `0.80–0.96`). Buying the favorite is mathematically identical to
fading the overpriced longshot on the other side.

Before any trade, a candidate must clear three liquidity gates:

1. 24-hour volume ≥ `min_volume24h`
2. Resting order-book depth ≥ `min_book_usd` on the side being hit
3. Bid/ask spread ≤ `max_spread`

Position size scales with **upside room**: the cheaper a favorite is inside
the band, the more room it has to converge to `1.0` at resolution, so it
earns a larger (still risk-capped) allocation. Sizing is bounded by
per-trade risk (% of equity), a max concurrent position count, per-market
exposure caps, a daily loss limit, and a max drawdown halt.

### Exits: built around resolution

The edge is realized when markets **resolve**, so exits favor holding
winners to settlement over scalping noise:

- **Resolution settlement** — positions are booked at the true `0`/`1`
  outcome, detected robustly even though the live order book disappears the
  instant a market resolves (price pinning + metadata cross-check, with a
  settlement fallback when the book 404s).
- **Take-profit** recycles capital once a position has run far enough.
- **Stop-loss** combines a wide percentage stop with an absolute price
  floor — a position is cut when its thesis breaks (the "favorite" is no
  longer favored), not on ordinary wobble.
- **Re-entry cooldown** prevents churn — the bot can't immediately re-buy a
  market it just exited.

Every decision, **taken or passed**, is written to the Reasoning Log in
plain English ("NO is the favorite at 0.86, inside the underpriced band
[0.75, 0.90] … spread 0.010, asks depth $301,559 … sizing $5.90"). This log
is both the audit trail and the corpus for the AI chat.

> A worked analysis of a real trading run — including how the exit rules
> were re-derived from losses — lives in
> [`TRADING_ANALYSIS.md`](./TRADING_ANALYSIS.md).

### Fill simulation: real order books, no shortcuts

`paper_engine_core.py` executes every order by **walking the live CLOB
book** level by level — consuming asks on buys, bids on sells — producing a
true volume-weighted average price and realistic slippage. Balance checks
and position updates run inside `BEGIN IMMEDIATE` SQLite transactions;
network I/O happens before the write lock is taken. Both the bots and the
manual-trading UI execute through this single code path.

---

## Multi-user design

### Four bots per account

There are four bot identities — **Kaladin, Adolin, Dalinar, Renarin** — each
with a distinct preset (balanced, aggressive, conservative, nimble). **Every
account owns an independent copy of all four**: its own paper money,
editable strategy parameters, agent loop, positions, logs, equity curve, and
reset. Four accounts = sixteen bots running in parallel. To keep that cheap,
all loops read market scans through a **shared TTL cache** — one Gamma API
fetch serves every bot in a scan window, so network cost doesn't scale with
the number of accounts.

### Roles

| Role | Sees | Controls |
|------|------|----------|
| **Spectator** (no login) | every account's bots, live P&L included | nothing — controls are visible but locked, and explain how to get an account |
| **User** | everything | their own four bots: start/stop, strategy, manual trades, funds, reset |
| **Admin** | everything | every account's bots; creates accounts via CLI |

Visibility is deliberately public — the product idea is a shared terminal
where accounts compete in the open; the restriction is on *actions*.
There is intentionally no self-registration: an admin creates each account
(and its four bots, provisioned stopped) with one CLI command.

**Auth implementation, stdlib only:** passwords are `hashlib.pbkdf2_hmac`
(600k iterations, per-user salt); sessions are opaque `secrets.token_urlsafe`
tokens stored server-side and delivered in an `httpOnly` / `SameSite=Lax`
cookie (`Secure` behind HTTPS) — revocable by deleting a row. Login failures
are uniform ("invalid username or password") and rate-limited per IP.

### Manual trading — the Markets page

`/markets` shows the same live scan the bots trade from, deliberately
**unfiltered** so a human can trade markets the bots skip. Each row shows
live YES/NO prices and volume, flags which markets would pass the bots'
baseline gates (as reference, not restriction), and can pull the live order
book on demand (best bid/ask, spread, walkable depth).

A manual trade lands in **one of your own bots' portfolios**, next to that
bot's automatic trades, and executes through the exact same fill engine.
Fills are tagged `source: "manual"` vs `"agent"` in the ledger and marked
with a ✋ badge in history and exports, so human picks and bot picks remain
distinguishable forever. Guardrails: cash balance is always enforced and an
empty book can't be filled — but the bots' strategy gates don't apply to
humans.

---

## Ask the Bot — local, grounded AI over the trading log

A chat tab that answers natural-language questions about the selected bot's
own trading — *"why did you buy NO on Spain winning the World Cup?"*,
*"what positions did you open today?"* — using a small instruction-tuned LLM
running **entirely on-device** via Apple's [MLX](https://github.com/ml-explore/mlx)
framework. No API calls, no cloud, no keys.

**The RAG pipeline** (`backend/rag/`):

1. **Chunk & embed** — every logged decision and trade becomes one
   self-contained text blob (timestamp, bot, action, prices, full reasoning),
   embedded with `all-MiniLM-L6-v2` into a `rag_chunks` table in the same
   SQLite file. Indexing is **incremental** — only rows newer than the last
   indexed id are ever embedded, so restarts re-embed nothing.
2. **Retrieve** — cosine similarity over in-memory numpy (vectors are
   L2-normalized, so scoring is a dot product), **blended with the most
   recent entries** so temporal questions like "what did you trade today?"
   work even when they share no vocabulary with individual log lines. At
   thousands of rows, a vector database would be pure overhead — measured,
   not assumed.
3. **Generate, grounded** — the model receives only the retrieved excerpts
   plus strict rules: answer solely from the excerpts, report what the log
   shows when it lacks an explicit explanation, and say *"I don't see
   anything in the log about that"* rather than filling gaps from general
   knowledge. A bot with an empty log skips the model entirely.
4. **Cite** — every answer returns its source chunks, rendered as an
   expandable list (type · timestamp · match score) under the reply, so any
   claim can be checked against the visible Reasoning Log.

The chat is **read-only by construction** — no code path leads from a
question to any trading action — and is open to spectators.

**Deployment honesty:** MLX runs only on Apple Silicon. Availability is
probed **once at server boot**, never per-request; on any other machine
(including the cloud deployment) the dashboard runs identically and the tab
explains the requirement instead of erroring. The generation model
(default `Qwen2.5-3B-Instruct`, 4-bit, ~1.8 GB) loads lazily into a
process-wide singleton on the first question and is reused for every
subsequent one, with generation serialized behind a lock.

---

## Engineering decisions worth noting

- **One fill path.** Bots and humans share the identical order-book
  simulation. A second "simplified" pricing path for manual trades would
  drift from reality — so it doesn't exist.
- **Database as the coordination layer.** Agent control state is persisted
  rows, not in-memory flags, so dashboard buttons work across processes and
  restarts with zero extra infrastructure.
- **Supervised, dynamic agent runtime.** A supervisor thread reconciles
  running loops against the portfolio table every few seconds — bots created
  while the server runs get loops without a restart; loops whose portfolio
  disappears retire themselves.
- **Shared scan cache with stampede protection.** Concurrent bot loops
  requesting the market scan block on one fetch and share the result,
  keeping API load flat as accounts grow.
- **Parallel mark-to-market.** Live position pricing fans out concurrently,
  so portfolio reads cost ~one network round trip regardless of position
  count.
- **Latency-honest UI.** Switching bots clears the view instantly, paints
  from cached marks in milliseconds (`?refresh=0` fast path), then follows
  up with live prices; every in-flight response is epoch-tagged and dropped
  if the user has moved on — stale data can never overwrite the current view.
- **Grounding over eloquence.** The AI feature treats hallucination as the
  primary failure mode: retrieval-only context, explicit refusal
  instructions, citations under every answer, and a no-model path when
  there's nothing to ground on.

---

## Getting started

```bash
git clone https://github.com/ElMariones/papermarket-tradebot.git
cd papermarket-tradebot
./run.sh
# open http://127.0.0.1:8765
```

One command starts the API, dashboard, and agent runtime. Requirements:
Python 3.11+ (`certifi` is installed automatically).

**Create accounts** (no signup form by design — admin provisions users):

```bash
python3 backend/create_user.py mario <password> --role admin
python3 backend/create_user.py sara  <password>            # regular user
```

Each command creates the account plus its four bots ($200 each by default,
`--balance N` to change), provisioned **stopped** until their owner presses
Start.

**Enable the AI chat** (optional, Apple Silicon only):

```bash
python3 -m venv ~/.polymarket-paper/venv
~/.polymarket-paper/venv/bin/pip install mlx-lm sentence-transformers 'transformers<5'
./run.sh   # detects and uses the venv automatically
```

The model (~1.8 GB) downloads from Hugging Face on the first question and is
cached afterwards.

### Deployment

Ships with a `Dockerfile` and `fly.toml` for a single-container cloud deploy
(SQLite on a mounted volume, agent runtime in-process). The AI chat tab
self-disables in the cloud — by design, since the point of the feature is
local inference.

---

## Configuration

| Setting | How |
|---------|-----|
| Accounts | `python3 backend/create_user.py <user> <pass> [--role admin] [--balance N]` |
| Strategy parameters | Dashboard → **Strategy** panel, per bot — every threshold the strategy uses, editable live |
| Position capacity | Dashboard → **Capacity** panel (syncs both the strategy cap and the hard risk cap) |
| Paper funds | Dashboard → **Funds** — deposits/withdrawals adjust cost basis so P&L stays honest |
| Starting balance | `TRADEBOT_START_BALANCE` (used when portfolios are first created) |
| DB location | `TRADEBOT_DB_PATH` (default `~/.polymarket-paper/portfolio.db`) |
| Port | `PORT` (default 8765) |
| LLM model | `TRADEBOT_LLM_MODEL` (default `mlx-community/Qwen2.5-3B-Instruct-4bit`) |
| Embedding model | `TRADEBOT_EMBED_MODEL` (default `all-MiniLM-L6-v2`) |
| Server interpreter | `TRADEBOT_PYTHON` (defaults to the ML venv when present, else `python3`) |
| Session lifetime | `SESSION_TTL_DAYS` constant in `backend/auth.py` (30 days) |

### CLI (no dashboard)

```bash
cd backend
python3 polymarket_client.py               # pull live markets + prices
python3 agent.py --name mario:Kaladin --cycles 3   # run agent cycles against live data
python3 worker.py                          # standalone agent runtime
python3 -m rag.ask mario:Kaladin "why did you pass on the NBA market?"  # grounded Q&A
```

---

## Project layout

```
.
├── backend/
│   ├── server.py             # stdlib REST API + static host + role checks
│   ├── engine.py             # ledger: funds, settings, equity, decision log
│   ├── paper_engine_core.py  # fill simulation (real order-book walking) + SQLite core
│   ├── strategy.py           # favorite-longshot decision engine + liquidity gates
│   ├── agent.py              # cycle: exits → scan → trade; supervised loops
│   ├── worker.py             # standalone agent-runtime entrypoint
│   ├── polymarket_client.py  # market discovery (Gamma API) + shared scan cache
│   ├── auth.py               # accounts + sessions (pbkdf2, opaque cookie tokens)
│   ├── create_user.py        # admin CLI: account + its four bots in one step
│   └── rag/                  # "Ask the Bot" — local MLX RAG
│       ├── llm.py            #   mlx-lm singleton: load once per process
│       ├── embed.py          #   log rows → text chunks → MiniLM embeddings
│       ├── index.py          #   incremental vector index + similarity/recency search
│       └── ask.py            #   retrieval + grounded prompt + cited answer
├── frontend/
│   ├── index.html · app.js   # trading terminal (tape, equity canvas, logs, AI chat)
│   ├── markets.html · markets.js   # live market browser + manual trading
│   ├── login.html · auth-ui.js     # sessions + spectator UX
│   └── styles.css            # one design system, light/dark
├── Dockerfile · fly.toml · run.sh · requirements.txt
└── TRADING_ANALYSIS.md       # strategy deep-dive from a real trading run
```

---

## Safety

`LIVE_TRADING = False` — and more importantly, **there is no live-execution
code path anywhere in this project**. Flipping the flag does nothing; a real
order could only be sent by deliberately building an order-submission path
that does not exist. Market data is real; execution is always simulated.

## License

Released as an educational reference for prediction-market mechanics,
order-book fill simulation, and local-first AI integration. Not financial
advice.
