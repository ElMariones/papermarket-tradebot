"""
engine.py — TradeBOT ledger/execution layer.

Reuses the battle-tested paper-trading engine shipped with the
`polymarket-paper-trader` skill (real CLOB order-book fill simulation,
SQLite persistence, risk validation) and extends it with:

  * an SSL fix (macOS python.org builds ship without CA certs — we route
    HTTPS through certifi)
  * add_funds()            — top up the paper balance from the dashboard
  * extra DB tables        — agent settings, agent control state, intraday
                             equity snapshots, decision log
  * helper accessors used by the API server

All market data is REAL (live Polymarket). All execution is SIMULATED.
No wallet, no keys, no real orders — ever.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi

# --- import the vendored core engine --------------------------------------
# paper_engine_core.py is a vendored copy of the polymarket-paper-trader skill
# engine, kept in-tree so the project is self-contained (Docker/deploy safe).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paper_engine_core as pe  # noqa: E402

# --- DB path: env-configurable so local dev and Fly volume can differ -----
# TRADEBOT_DB_PATH (e.g. /data/portfolio.db on Fly) overrides the default
# ~/.polymarket-paper/portfolio.db without any code change.
_db_env = os.environ.get("TRADEBOT_DB_PATH")
if _db_env:
    pe.DB_PATH = Path(_db_env)
    pe.DB_DIR = pe.DB_PATH.parent

# --- SSL fix: make the engine's urllib calls verify via certifi -----------
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_orig_urlopen = urllib.request.urlopen


def _patched_urlopen(req, timeout=15, **kw):
    return _orig_urlopen(req, timeout=timeout, context=_SSL_CTX, **kw)


# paper_engine did `from urllib.request import urlopen`, so patch its binding.
pe.urlopen = _patched_urlopen

# Re-export the core engine surface so the rest of the app imports from here.
DB_PATH = pe.DB_PATH
DB_DIR = pe.DB_DIR
GAMMA_API = pe.GAMMA_API
CLOB_API = pe.CLOB_API
DEFAULT_RISK = pe.DEFAULT_RISK

fetch_orderbook = pe.fetch_orderbook
fetch_midpoint = pe.fetch_midpoint
fetch_price = pe.fetch_price
lookup_market = pe.lookup_market
init_portfolio = pe.init_portfolio
get_portfolio = pe.get_portfolio
place_order = pe.place_order
close_position = pe.close_position
get_trades = pe.get_trades
take_snapshot = pe.take_snapshot
api_get = pe._api_get  # used by the discovery client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Extra schema (lives in the same SQLite DB as the engine)
# --------------------------------------------------------------------------

def _conn():
    return pe._get_db()


def _ensure_extra_schema():
    conn = _conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_settings (
                portfolio_name TEXT PRIMARY KEY,
                config         TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_state (
                portfolio_name TEXT PRIMARY KEY,
                status         TEXT NOT NULL DEFAULT 'stopped',  -- running|paused|stopped
                last_cycle_at  TEXT,
                cycles         INTEGER NOT NULL DEFAULT 0,
                last_message   TEXT,
                updated_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_name TEXT NOT NULL,
                ts             TEXT NOT NULL,
                cash_balance   REAL NOT NULL,
                positions_value REAL NOT NULL,
                total_value    REAL NOT NULL,
                pnl            REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_equity_pf_ts
                ON equity_snapshots(portfolio_name, ts);

            CREATE TABLE IF NOT EXISTS decisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_name TEXT NOT NULL,
                ts             TEXT NOT NULL,
                token_id       TEXT,
                market_question TEXT,
                signal         TEXT,         -- BUY_YES | BUY_NO | EXIT | PASS
                acted          INTEGER NOT NULL DEFAULT 0,
                confidence     REAL,
                reasoning      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_pf_ts
                ON decisions(portfolio_name, ts);

            CREATE TABLE IF NOT EXISTS hourly_reports (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_name TEXT NOT NULL,
                ts             TEXT NOT NULL,
                total_value    REAL NOT NULL,
                cash_balance   REAL NOT NULL,
                positions_value REAL NOT NULL,
                realized_pnl   REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                pnl_pct        REAL NOT NULL,
                num_positions  INTEGER NOT NULL,
                win_rate       REAL NOT NULL,
                closed_trades  INTEGER NOT NULL,
                total_trades   INTEGER NOT NULL,
                decisions_last_hour INTEGER NOT NULL DEFAULT 0,
                trades_last_hour INTEGER NOT NULL DEFAULT 0,
                detail         TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_hourly_pf_ts
                ON hourly_reports(portfolio_name, ts);

            -- Accounts (auth.py owns the logic; the tables live here so every
            -- entrypoint that writes portfolios sees them — the portfolios
            -- owner_user_id FK below needs users to exist first).
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'user',  -- 'admin' | 'user'
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        # Column migrations for DBs created before accounts/manual trading.
        # ALTER TABLE ADD COLUMN raises if the column exists, so probe first.
        def _add_column(table: str, ddl: str, col: str):
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        # 'bot' = one of the four autonomous profiles; 'user' = a personal
        # portfolio owned by an account, traded only by hand (no agent loop).
        _add_column("portfolios", "owner_type TEXT NOT NULL DEFAULT 'bot'", "owner_type")
        _add_column("portfolios", "owner_user_id INTEGER REFERENCES users(id)", "owner_user_id")
        # who initiated each fill: the agent loop or a human on the dashboard
        _add_column("trades", "source TEXT NOT NULL DEFAULT 'agent'", "source")
        conn.commit()
    finally:
        conn.close()


_ensure_extra_schema()


# --------------------------------------------------------------------------
# Default strategy / agent configuration
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    # loop
    "scan_interval_sec": 60,
    "markets_per_scan": 60,        # how many top-volume markets to evaluate
    # risk / sizing (mirrors portfolio risk_config but drives the strategy)
    "risk_per_trade_pct": 0.05,    # 5% of total value, scaled by confidence
    "max_concurrent_positions": 10,
    "min_trade_usd": 2.0,
    # strategy: favorite-longshot bias + liquidity filter
    "confidence_threshold": 0.55,  # min confidence to act
    "fav_low": 0.80,               # buy YES favorites priced in [fav_low, fav_high]
    "fav_high": 0.96,
    "longshot_thresh": 0.06,       # fade longshots: buy NO when YES price < this
    "max_entry_price": 0.97,       # never pay above this for a share (keeps upside)
    "min_volume24h": 5000.0,       # liquidity gate
    "min_book_usd": 50.0,          # min resting depth on the side we hit
    "max_spread": 0.06,            # skip wide markets (bid/ask spread)
    # exits
    # The favorite-longshot edge is realized at RESOLUTION, so exits are tuned
    # to hold winners to convergence and only cut a position when its thesis is
    # broken — not on ordinary intraday noise (see TRADING_ANALYSIS.md).
    "take_profit_pct": 0.20,       # recycle capital when price up 20% vs entry
    "stop_loss_pct": 0.40,         # wide % stop: let a strong favorite breathe
    "stop_loss_price": 0.50,       # hard floor: cut once our side is no longer the favorite
    # resolution settlement (book disappears the moment a market resolves)
    "resolve_hi": 0.99,            # our side priced >= this => treat as won, settle 1.0
    "resolve_lo": 0.01,            # our side priced <= this => treat as lost, settle 0.0
    "reentry_cooldown_min": 180,   # don't re-enter a market we just exited (min)
    # category filter: list of market-class tags to skip entirely (see
    # strategy.classify_market). Empty = trade everything. Tags: "inplay",
    # "single_match". Enabled on some profiles after log analysis showed
    # single-match/in-play sports caused ~all of the net loss.
    "exclude_categories": [],
}


# --------------------------------------------------------------------------
# Bots — the four bot identities: Kaladin, Adolin, Dalinar, Renarin.
#
# EVERY ACCOUNT OWNS ITS OWN COPY OF ALL FOUR. A portfolio is one user's copy
# of one bot, named "<username>:<Bot>" (e.g. "mario:Kaladin"), with its own
# money, editable strategy settings, agent loop, trades, logs and reset.
# Sara can make her Dalinar aggressive and fund it with $50 without touching
# Mario's Dalinar. Four accounts = sixteen independently running bots.
#
# Everything in the engine is keyed by portfolio name, so this composes for
# free: each (user, bot) pair is just a distinct portfolio_name. The presets
# below are per bot IDENTITY — the starting point every user's copy begins
# from. Plain unprefixed names ("Kaladin") exist only on a fresh install with
# no accounts (demo mode); the first admin account claims them, history and
# all, and they become that admin's set.
# --------------------------------------------------------------------------

PROFILES = ["Kaladin", "Adolin", "Dalinar", "Renarin"]  # bot identities


def bot_identity(portfolio_name: str) -> str:
    """'mario:Kaladin' -> 'Kaladin'; legacy plain 'Kaladin' -> itself."""
    bot = portfolio_name.split(":", 1)[1] if ":" in portfolio_name else portfolio_name
    return bot if bot in PROFILES else PROFILES[0]


def portfolio_name_for(username: str, bot: str) -> str:
    return f"{username}:{bot}"

PROFILE_META = {
    # Kaladin — balanced harvester, now NO single-match/in-play sports.
    # Filtered twin of the tuned baseline: trades only markets that converge by
    # resolution (elections, "out by DATE", tournament outrights). See log
    # analysis — single-match/in-play sports were the whole net loss.
    "Kaladin": {
        "start_balance": 200.0,
        "blurb": "Balanced, no single-match/in-play sports — convergence only.",
        "settings": {
            "fav_low": 0.80, "fav_high": 0.96, "max_entry_price": 0.97,
            "risk_per_trade_pct": 0.05, "max_concurrent_positions": 12,
            "confidence_threshold": 0.55, "scan_interval_sec": 60,
            # Scans deep (250) so non-sports convergence markets surface below
            # the hot single-match sports the filter drops; the category gate
            # short-circuits excluded markets before any order-book fetch, so
            # the extra depth is cheap.
            "markets_per_scan": 250, "min_volume24h": 5000.0, "min_book_usd": 50.0,
            "max_spread": 0.06, "take_profit_pct": 0.20, "stop_loss_pct": 0.40,
            "stop_loss_price": 0.50,
            "exclude_categories": ["inplay", "single_match"],
        },
    },
    # Adolin — aggressive, wide net, but NO single-match/in-play sports.
    # It took the worst soccer damage in the logs (Uruguay-halftime -$15,
    # Egypt-moneyline -$13); the in-play exclusion removes exactly that tail
    # while keeping the wide-band aggression on convergence markets.
    "Adolin": {
        "start_balance": 200.0,
        "blurb": "Aggressive, no single-match/in-play sports — wide band, convergence only.",
        "settings": {
            "fav_low": 0.70, "fav_high": 0.97, "max_entry_price": 0.97,
            "risk_per_trade_pct": 0.08, "max_concurrent_positions": 20,
            "confidence_threshold": 0.50, "scan_interval_sec": 45,
            # Aggressive + filtered: scans the deepest (300) to feed its wide
            # band and high position count from the convergence-only universe.
            "markets_per_scan": 300, "min_volume24h": 3000.0, "min_book_usd": 40.0,
            "max_spread": 0.08, "take_profit_pct": 0.25, "stop_loss_pct": 0.45,
            "stop_loss_price": 0.45,
            "exclude_categories": ["inplay", "single_match"],
        },
    },
    # Dalinar — conservative: only deep favorites, tight liquidity, small size.
    "Dalinar": {
        "start_balance": 200.0,
        "blurb": "Conservative — deep favorites only, tight liquidity.",
        "settings": {
            "fav_low": 0.88, "fav_high": 0.97, "max_entry_price": 0.98,
            "risk_per_trade_pct": 0.03, "max_concurrent_positions": 8,
            "confidence_threshold": 0.65, "scan_interval_sec": 90,
            "markets_per_scan": 50, "min_volume24h": 20000.0, "min_book_usd": 200.0,
            "max_spread": 0.03, "take_profit_pct": 0.15, "stop_loss_pct": 0.50,
            "stop_loss_price": 0.60,
        },
    },
    # Renarin — nimble mid-band fader: smaller, faster, quicker profit-taking.
    "Renarin": {
        "start_balance": 200.0,
        "blurb": "Nimble — mid-band favorites, fast scan, quick profits.",
        "settings": {
            "fav_low": 0.75, "fav_high": 0.90, "max_entry_price": 0.93,
            "risk_per_trade_pct": 0.04, "max_concurrent_positions": 10,
            "confidence_threshold": 0.55, "scan_interval_sec": 45,
            "markets_per_scan": 70, "min_volume24h": 5000.0, "min_book_usd": 60.0,
            "max_spread": 0.05, "take_profit_pct": 0.18, "stop_loss_pct": 0.35,
            "stop_loss_price": 0.45,
        },
    },
}


def active_bot_names() -> list[str]:
    """Names of every active bot portfolio (all users' copies + any
    unclaimed demo set), in creation order."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name FROM portfolios WHERE active = 1 ORDER BY id"
        ).fetchall()
        return [r["name"] for r in rows]
    finally:
        conn.close()


def resolve_profile(name: str | None) -> str:
    """Normalize an incoming portfolio name to an existing one.
    Default = the first active portfolio (the original Kaladin — or, after
    it's claimed, the admin's Kaladin)."""
    names = active_bot_names()
    if name in names:
        return name
    return names[0] if names else PROFILES[0]


def portfolio_owner(name: str) -> dict:
    """Ownership info for a portfolio: {'owner_type', 'owner_user_id'}."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT owner_type, owner_user_id FROM portfolios "
            "WHERE name = ? AND active = 1 ORDER BY id DESC LIMIT 1", (name,),
        ).fetchone()
        if row:
            return {"owner_type": row["owner_type"],
                    "owner_user_id": row["owner_user_id"]}
        return {"owner_type": "bot", "owner_user_id": None}
    finally:
        conn.close()


def _risk_for(settings: dict) -> dict:
    """Hard risk caps derived from a bot's strategy settings (same shape the
    server has always used when creating the four demo bots)."""
    return {
        "max_position_pct": max(0.05, float(settings["risk_per_trade_pct"])),
        "max_concurrent_positions": int(settings["max_concurrent_positions"]),
        "max_single_market_pct": 0.10, "daily_loss_limit_pct": 0.05,
        "max_drawdown_pct": 0.30, "human_approval_pct": 0.20,
    }


def create_bot_for_user(username: str, user_id: int, bot: str,
                        balance: float | None = None) -> dict:
    """Create one user's copy of one bot, starting from that bot's preset,
    stopped (it trades only when its owner presses Start)."""
    name = portfolio_name_for(username, bot)
    if portfolio_exists(name):
        raise ValueError(f"Portfolio '{name}' already exists")
    if balance is None:
        balance = start_balance_for(name)
    pf = init_portfolio(balance, name, _risk_for(_defaults_for(name)))
    conn = _conn()
    try:
        conn.execute(
            "UPDATE portfolios SET owner_type = 'bot', owner_user_id = ? "
            "WHERE id = ?", (user_id, pf["portfolio_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    save_settings(name, {})          # persist the preset + sync risk caps
    set_agent_status(name, "stopped", message="created — press Start to trade")
    record_equity(name)              # seed the equity curve
    return pf


def create_user_bots(username: str, user_id: int,
                     balance: float | None = None) -> list[str]:
    """Create the user's full set of four bots (skipping any that exist)."""
    made = []
    for bot in PROFILES:
        if not portfolio_exists(portfolio_name_for(username, bot)):
            create_bot_for_user(username, user_id, bot, balance)
            made.append(portfolio_name_for(username, bot))
    return made


# Tables keyed by portfolio NAME (portfolios itself is handled separately;
# positions/trades/daily_snapshots key by portfolio_id and never need renames).
_NAME_KEYED_TABLES = ("agent_settings", "agent_state", "equity_snapshots",
                      "decisions", "hourly_reports")


def claim_legacy_bots(user_id: int, username: str) -> list[str]:
    """
    Assign the original unowned demo set (plain 'Kaladin' ... names) to an
    admin account, HISTORY AND ALL: renames every row keyed by the old name
    to '<username>:<Bot>' and sets ownership. Idempotent — does nothing once
    no unowned plain-named bots remain.
    """
    claimed = []
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for bot in PROFILES:
            row = conn.execute(
                "SELECT 1 FROM portfolios WHERE name = ? AND active = 1 "
                "AND owner_user_id IS NULL", (bot,)).fetchone()
            if not row:
                continue
            new = portfolio_name_for(username, bot)
            # never rename onto an existing portfolio — if the admin already
            # has this bot, the unowned one is a duplicate artifact and gets
            # retired by the migration sweep instead
            if conn.execute("SELECT 1 FROM portfolios WHERE name = ? AND active = 1",
                            (new,)).fetchone():
                continue
            # rename every generation of the portfolio (active + reset history)
            conn.execute(
                "UPDATE portfolios SET name = ?, owner_user_id = ?, "
                "owner_type = 'bot' WHERE name = ?", (new, user_id, bot))
            for tbl in _NAME_KEYED_TABLES:
                conn.execute(
                    f"UPDATE {tbl} SET portfolio_name = ? WHERE portfolio_name = ?",
                    (new, bot))
            claimed.append(new)
        conn.commit()
    finally:
        conn.close()
    return claimed


def migrate_to_per_user_bots() -> None:
    """
    One-shot boot migration to the per-user-bot-sets model:
      1. retire any personal manual portfolios from the short-lived
         accounts-v1 design (deactivated, data kept)
      2. claim the unowned demo set for the first admin, if one exists
      3. make sure every account has its four bots (created stopped)
    """
    conn = _conn()
    try:
        conn.execute(
            "UPDATE portfolios SET active = 0 "
            "WHERE owner_type = 'user' AND active = 1")
        conn.commit()
        admin = conn.execute(
            "SELECT id, username FROM users WHERE role = 'admin' "
            "ORDER BY id LIMIT 1").fetchone()
        users = conn.execute(
            "SELECT id, username FROM users ORDER BY id").fetchall()
    finally:
        conn.close()
    if admin:
        for name in claim_legacy_bots(admin["id"], admin["username"]):
            print(f"Claimed legacy bot as {name}")
    if users:
        # Any unowned plain-named set still active once accounts exist is a
        # deploy-race artifact (old code re-creating the demo set after the
        # admin claimed the original — the "house bots" duplicate). The claim
        # above already rescued anything claimable; retire the rest.
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE portfolios SET active = 0 "
                "WHERE owner_user_id IS NULL AND active = 1")
            conn.commit()
            if cur.rowcount:
                print(f"Retired {cur.rowcount} orphaned unowned demo portfolio(s)")
        finally:
            conn.close()
    for u in users:
        for name in create_user_bots(u["username"], u["id"]):
            print(f"Created {name} (stopped)")


def _defaults_for(name: str) -> dict:
    """Default strategy for a portfolio = global defaults + the preset of its
    bot IDENTITY ('sara:Dalinar' starts from Dalinar's preset). Each copy is
    then edited independently via agent_settings."""
    preset = PROFILE_META.get(bot_identity(name), {}).get("settings", {})
    return {**DEFAULT_SETTINGS, **preset}


def start_balance_for(name: str) -> float:
    return float(PROFILE_META.get(bot_identity(name), {}).get("start_balance",
                 os.environ.get("TRADEBOT_START_BALANCE", "200")))


def get_settings(name: str = "default") -> dict:
    base = _defaults_for(name)
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT config FROM agent_settings WHERE portfolio_name = ?", (name,)
        ).fetchone()
        if row:
            return {**base, **json.loads(row["config"])}
        return dict(base)
    finally:
        conn.close()


def get_profiles_overview() -> list[dict]:
    """Lightweight per-portfolio status for the dashboard selector (no live
    price refresh — the active profile's main view handles mark-to-market).
    Every account's copy of every bot, grouped by owner (owners in account
    order, bots in canonical order within each owner); an unclaimed demo set
    sorts first with owner = null."""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT p.name, p.owner_user_id, u.username AS owner
               FROM portfolios p LEFT JOIN users u ON u.id = p.owner_user_id
               WHERE p.active = 1 ORDER BY p.owner_user_id IS NOT NULL,
                     p.owner_user_id, p.id"""
        ).fetchall()
    finally:
        conn.close()

    def bot_order(r):
        b = bot_identity(r["name"])
        return PROFILES.index(b) if b in PROFILES else 99

    grouped = sorted(rows, key=lambda r: (r["owner_user_id"] is not None,
                                          r["owner_user_id"] or 0, bot_order(r)))
    out = []
    for r in grouped:
        name = r["name"]
        bot = bot_identity(name)
        base = {"name": name, "bot": bot,
                "owner_user_id": r["owner_user_id"], "owner": r["owner"],
                "blurb": PROFILE_META.get(bot, {}).get("blurb", "")}
        try:
            s = compute_summary(name, refresh=False)
            pf = s["portfolio"]
            out.append({
                **base,
                "status": s["agent"]["status"],
                "cycles": s["agent"].get("cycles", 0),
                "total_value": pf["total_value"], "pnl_pct": pf["pnl_pct"],
                "num_positions": pf["num_open_positions"],
            })
        except Exception:
            out.append({**base, "status": "stopped", "cycles": 0,
                        "total_value": None, "pnl_pct": None, "num_positions": 0})
    return out


def save_settings(name: str, updates: dict) -> dict:
    cfg = get_settings(name)
    # only accept known keys, coerce numeric types
    for k, v in updates.items():
        if k not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[k]
        try:
            cfg[k] = type(default)(v)
        except (TypeError, ValueError):
            cfg[k] = v
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO agent_settings (portfolio_name, config, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(portfolio_name) DO UPDATE SET
                 config = excluded.config, updated_at = excluded.updated_at""",
            (name, json.dumps(cfg), _now()),
        )
        # Keep the portfolio's hard risk_config in sync with the strategy
        # settings, so raising the position cap (or per-trade risk) in the UI
        # actually takes effect — place_order validates against risk_config,
        # not these settings.
        pf = conn.execute(
            "SELECT id, risk_config FROM portfolios WHERE name = ? AND active = 1 "
            "ORDER BY id DESC LIMIT 1", (name,),
        ).fetchone()
        if pf:
            risk = json.loads(pf["risk_config"])
            risk["max_concurrent_positions"] = int(cfg["max_concurrent_positions"])
            # per-trade hard cap must be >= sizing target, or trades get rejected
            risk["max_position_pct"] = max(
                float(cfg["risk_per_trade_pct"]), risk.get("max_position_pct", 0.05))
            # approval + single-market caps must allow the per-trade size through
            risk["human_approval_pct"] = max(
                risk.get("human_approval_pct", 0.20), risk["max_position_pct"] + 0.01)
            risk["max_single_market_pct"] = max(
                risk.get("max_single_market_pct", 0.10), risk["max_position_pct"] + 0.01)
            conn.execute(
                "UPDATE portfolios SET risk_config = ?, updated_at = ? WHERE id = ?",
                (json.dumps(risk), _now(), pf["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return cfg


# --------------------------------------------------------------------------
# Agent control state
# --------------------------------------------------------------------------

def get_agent_state(name: str = "default") -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM agent_state WHERE portfolio_name = ?", (name,)
        ).fetchone()
        if row:
            return dict(row)
        return {
            "portfolio_name": name, "status": "stopped",
            "last_cycle_at": None, "cycles": 0, "last_message": None,
        }
    finally:
        conn.close()


def set_agent_status(name: str, status: str, message: str | None = None) -> dict:
    assert status in ("running", "paused", "stopped")
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO agent_state
                 (portfolio_name, status, last_message, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(portfolio_name) DO UPDATE SET
                 status = excluded.status,
                 last_message = COALESCE(excluded.last_message, agent_state.last_message),
                 updated_at = excluded.updated_at""",
            (name, status, message, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_agent_state(name)


def mark_cycle(name: str, message: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO agent_state
                 (portfolio_name, status, last_cycle_at, cycles, last_message, updated_at)
               VALUES (?, 'running', ?, 1, ?, ?)
               ON CONFLICT(portfolio_name) DO UPDATE SET
                 last_cycle_at = excluded.last_cycle_at,
                 cycles = agent_state.cycles + 1,
                 last_message = excluded.last_message,
                 updated_at = excluded.updated_at""",
            (name, _now(), message, _now()),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Equity snapshots + decision log
# --------------------------------------------------------------------------

def record_equity(name: str = "default") -> dict:
    st = get_portfolio(name, refresh_prices=True)
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO equity_snapshots
                 (portfolio_name, ts, cash_balance, positions_value, total_value, pnl)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, _now(), st["cash_balance"], st["positions_value"],
             st["total_value"], st["pnl"]),
        )
        conn.commit()
    finally:
        conn.close()
    return st


def get_equity_curve(name: str = "default", limit: int = 1000) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT ts, total_value, cash_balance, positions_value, pnl
               FROM equity_snapshots
               WHERE portfolio_name = ?
               ORDER BY ts ASC
               LIMIT ?""",
            (name, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_decision(name: str, token_id, market_question, signal,
                 acted: bool, confidence, reasoning) -> None:
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO decisions
                 (portfolio_name, ts, token_id, market_question, signal,
                  acted, confidence, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, _now(), token_id, market_question, signal,
             1 if acted else 0, confidence, reasoning),
        )
        conn.commit()
    finally:
        conn.close()


def get_decisions(name: str = "default", limit: int = 100) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT * FROM decisions WHERE portfolio_name = ?
               ORDER BY id DESC LIMIT ?""",
            (name, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Add funds
# --------------------------------------------------------------------------

def _move_funds(amount: float, name: str, sign: int) -> dict:
    """
    Move paper capital in (sign=+1, deposit) or out (sign=-1, withdraw) of a
    profile's CASH balance.

    Both directions adjust the cost basis (starting_balance) by the same amount
    so P&L stays honest — a deposit isn't fake profit, and a withdrawal isn't a
    fake loss. Withdrawals come from cash only (open positions aren't liquid),
    so you can never pull out more cash than the profile is holding.
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pf = pe._active_portfolio(conn, name)
        if sign < 0 and amount > pf["cash_balance"] + 1e-9:
            conn.rollback()
            raise ValueError(
                f"Cannot withdraw ${amount:,.2f}: only ${pf['cash_balance']:,.2f} "
                f"cash available (open positions aren't liquid).")
        delta = sign * amount
        new_balance = pf["cash_balance"] + delta
        new_start = max(0.0, pf["starting_balance"] + delta)
        new_peak = max(round(new_balance, 4), pf["peak_value"] + delta)
        conn.execute(
            """UPDATE portfolios
               SET cash_balance = ?, starting_balance = ?, peak_value = ?, updated_at = ?
               WHERE id = ?""",
            (round(new_balance, 4), round(new_start, 4), round(new_peak, 4),
             _now(), pf["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return get_portfolio(name, refresh_prices=False)


def add_funds(amount: float, name: str = "default") -> dict:
    """Deposit paper capital into a profile's cash balance."""
    return _move_funds(amount, name, sign=+1)


def withdraw_funds(amount: float, name: str = "default") -> dict:
    """Withdraw paper capital from a profile's cash balance."""
    return _move_funds(amount, name, sign=-1)


def manual_trade(name: str, token_id: str, side: str, action: str,
                 amount: float | None, username: str = "") -> dict:
    """
    Execute a human-initiated trade through the SAME order-book fill
    simulation the bot uses (place_order / close_position walk the real CLOB
    book) — no separate pricing path, so manual fills match agent fills.

    House rules only: a buy must fit the cash balance (enforced inside
    place_order) and the book must have depth to walk (both paths raise on an
    empty book). The bot's strategy confidence bar and liquidity gates do NOT
    apply — those are the agent's rules, not the market's. force=True skips
    the agent's portfolio risk guards for the same reason.
    """
    side = (side or "").upper()
    action = (action or "").lower()
    if side not in ("YES", "NO"):
        raise ValueError("side must be YES or NO")
    if action not in ("buy", "sell"):
        raise ValueError("action must be buy or sell")

    who = f" by {username}" if username else ""
    if action == "buy":
        if not amount or amount <= 0:
            raise ValueError("amount must be positive")
        result = place_order(
            token_id=token_id, side=side, size=float(amount),
            reasoning=f"Manual buy{who} from the Markets page.",
            portfolio_name=name, force=True, source="manual")
        signal = f"BUY_{side}"
        note = (f"MANUAL BUY {side} ${amount:.2f} @ {result['avg_price']:.3f} "
                f"({result['shares']:.2f} shares)")
    else:
        result = close_position(
            token_id, side, name,
            reasoning=f"Manual close{who} from the dashboard.",
            source="manual")
        r = result if isinstance(result, dict) else result[0]
        signal = "EXIT"
        note = (f"MANUAL SELL {side} {r['shares_sold']:.2f} shares "
                f"@ {r['avg_sell_price']:.3f} (P&L ${r['realized_pnl']:+.2f})")

    market_q = result["market"] if isinstance(result, dict) else result[0]["market"]
    log_decision(name, token_id, market_q, signal, True, None, note)
    record_equity(name)  # manual portfolios have no cycle to snapshot for them
    return result


def settle_position(token_id: str, side: str, price: float,
                    name: str = "default", reasoning: str = "",
                    source: str = "agent") -> dict:
    """
    Force-close an open position at a fixed price (no order-book walk).

    Used to settle positions when a market has RESOLVED — the live book is
    gone, but the outcome is known (price 1.0 if our side won, 0.0 if it lost).
    """
    side = side.upper()
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pf = pe._active_portfolio(conn, name)
        pid = pf["id"]
        pos = conn.execute(
            """SELECT * FROM positions
               WHERE portfolio_id = ? AND token_id = ? AND side = ? AND closed = 0""",
            (pid, token_id, side),
        ).fetchone()
        if not pos:
            conn.rollback()
            raise RuntimeError(f"No open {side} position for {token_id}")
        pos = dict(pos)
        shares = pos["shares"]
        proceeds = shares * price
        pnl = (price - pos["avg_entry"]) * shares
        now = _now()
        conn.execute(
            "UPDATE positions SET closed = 1, closed_at = ?, current_price = ?, updated_at = ? WHERE id = ?",
            (now, price, now, pos["id"]),
        )
        new_balance = pf["cash_balance"] + proceeds
        conn.execute(
            "UPDATE portfolios SET cash_balance = ?, updated_at = ? WHERE id = ?",
            (round(new_balance, 4), now, pid),
        )
        conn.execute(
            """INSERT INTO trades
               (portfolio_id, token_id, market_question, side, action,
                shares, price, fee, total_cost, reasoning, executed_at,
                entry_avg, source)
               VALUES (?, ?, ?, ?, 'SELL', ?, ?, 0, ?, ?, ?, ?, ?)""",
            (pid, token_id, pos["market_question"], side, round(shares, 4),
             round(price, 6), round(proceeds, 4), reasoning, now,
             pos["avg_entry"], source),
        )
        conn.commit()
        return {
            "status": "settled", "side": side, "token_id": token_id,
            "market": pos["market_question"], "shares_sold": round(shares, 4),
            "settle_price": price, "avg_entry_price": pos["avg_entry"],
            "realized_pnl": round(pnl, 4), "new_balance": round(new_balance, 4),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def portfolio_exists(name: str = "default") -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM portfolios WHERE name = ? AND active = 1 LIMIT 1", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def export_all(name: str = "default") -> dict:
    """
    One complete, self-contained snapshot of everything: live portfolio +
    results summary, every trade, the full decision/reasoning log, the equity
    curve, all hourly reports, and the current strategy settings. Returned as a
    plain dict ready to serialize to a downloadable JSON file.
    """
    summary = compute_summary(name, refresh=True)
    trades = get_trades(name, 100000)
    # add realized P&L per SELL row for convenience in analysis
    for t in trades:
        if t["action"] == "SELL" and t.get("entry_avg") is not None:
            t["realized_pnl"] = round((t["price"] - t["entry_avg"]) * t["shares"], 4)
    return {
        "export_version": 1,
        "app": "TradeBOT — Polymarket paper trading",
        "exported_at": _now(),
        "portfolio_name": name,
        "results": {
            "starting_balance": summary["portfolio"]["starting_balance"],
            "total_value": summary["portfolio"]["total_value"],
            "cash_balance": summary["portfolio"]["cash_balance"],
            "positions_value": summary["portfolio"]["positions_value"],
            "total_pnl": round(summary["realized_pnl"] + summary["unrealized_pnl"], 2),
            "realized_pnl": summary["realized_pnl"],
            "unrealized_pnl": summary["unrealized_pnl"],
            "pnl_pct": summary["portfolio"]["pnl_pct"],
            "drawdown_pct": summary["portfolio"]["drawdown_pct"],
            "win_rate": summary["win_rate"],
            "closed_trades": summary["closed_trades"],
            "total_trades": summary["total_trades"],
        },
        "agent_state": summary["agent"],
        "settings": get_settings(name),
        "open_positions": summary["portfolio"]["positions"],
        "trades": trades,
        "decisions": get_decisions(name, 100000),
        "equity_curve": get_equity_curve(name, 100000),
        "hourly_reports": get_hourly_reports(name, 100000),
    }


def reset_all(name: str = "default", balance: float | None = None,
              keep_running: bool = True) -> dict:
    """
    Full reset: wipe ALL trading data for this portfolio (positions, trades,
    snapshots, equity curve, decisions, hourly reports) and re-create a fresh
    portfolio at `balance`. Strategy/agent SETTINGS are preserved (they're your
    tuning, not data). Returns the fresh portfolio state.
    """
    if balance is None:
        balance = start_balance_for(name)
    if balance <= 0:
        raise ValueError("Reset balance must be positive")

    # Preserve the prior settings (so a raised position cap survives reset)
    # and ownership (so a user's personal portfolio stays theirs).
    prior_settings = get_settings(name)
    owner = portfolio_owner(name)
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM portfolios WHERE name = ?", (name,)).fetchall()]
        if ids:
            qmarks = ",".join("?" * len(ids))
            for tbl in ("positions", "trades", "daily_snapshots"):
                conn.execute(f"DELETE FROM {tbl} WHERE portfolio_id IN ({qmarks})", ids)
        for tbl in ("equity_snapshots", "decisions", "hourly_reports", "agent_state"):
            conn.execute(f"DELETE FROM {tbl} WHERE portfolio_name = ?", (name,))
        conn.execute("UPDATE portfolios SET active = 0 WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()

    risk = {
        "max_position_pct": max(0.05, float(prior_settings["risk_per_trade_pct"])),
        "max_concurrent_positions": int(prior_settings["max_concurrent_positions"]),
        "max_single_market_pct": 0.10, "daily_loss_limit_pct": 0.05,
        "max_drawdown_pct": 0.30, "human_approval_pct": 0.20,
    }
    pf_new = init_portfolio(balance, name, risk)
    if owner["owner_user_id"] is not None:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE portfolios SET owner_type = ?, owner_user_id = ? "
                "WHERE id = ?", (owner["owner_type"], owner["owner_user_id"],
                                 pf_new["portfolio_id"]),
            )
            conn.commit()
        finally:
            conn.close()
    save_settings(name, {})  # re-sync risk_config with preserved settings
    record_equity(name)      # seed the new equity curve
    set_agent_status(name, "running" if keep_running else "stopped",
                     message="portfolio reset")
    return get_portfolio(name, refresh_prices=False)


# --------------------------------------------------------------------------
# Summary + hourly performance reports
# --------------------------------------------------------------------------

def compute_summary(name: str = "default", refresh: bool = True) -> dict:
    """Portfolio + agent state + realized/unrealized P&L and win rate."""
    pf = get_portfolio(name, refresh_prices=refresh)
    state = get_agent_state(name)
    trades = get_trades(name, 5000)
    sells = [t for t in trades if t["action"] == "SELL" and t.get("entry_avg") is not None]
    realized = sum((t["price"] - t["entry_avg"]) * t["shares"] for t in sells)
    wins = sum(1 for t in sells if (t["price"] - t["entry_avg"]) > 0)
    win_rate = (wins / len(sells) * 100) if sells else 0.0
    unrealized = sum(p["unrealized_pnl"] for p in pf["positions"])
    return {
        "portfolio": pf,
        "agent": state,
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "closed_trades": len(sells),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
    }


def _count_since(table: str, name: str, since_iso: str) -> int:
    conn = _conn()
    try:
        col = "executed_at" if table == "trades" else "ts"
        idcol = "portfolio_id" if table == "trades" else "portfolio_name"
        if table == "trades":
            pf = pe._active_portfolio(conn, name)
            key = pf["id"]
        else:
            key = name
        row = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE {idcol} = ? AND {col} >= ?",
            (key, since_iso),
        ).fetchone()
        return row["c"] if row else 0
    finally:
        conn.close()


def record_hourly_report(name: str = "default") -> dict:
    """Persist a rich hourly performance snapshot and return it."""
    from datetime import timedelta
    s = compute_summary(name, refresh=True)
    pf = s["portfolio"]
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    dec_1h = _count_since("decisions", name, hour_ago)
    trd_1h = _count_since("trades", name, hour_ago)
    top = sorted(pf["positions"], key=lambda p: abs(p["unrealized_pnl"]), reverse=True)[:5]
    detail = json.dumps({
        "top_positions": [
            {"q": (p["market_question"] or "")[:60], "side": p["side"],
             "entry": p["avg_entry"], "mark": p["current_price"],
             "upnl": p["unrealized_pnl"]} for p in top],
    })
    rec = {
        "ts": _now(),
        "total_value": pf["total_value"],
        "cash_balance": pf["cash_balance"],
        "positions_value": pf["positions_value"],
        "realized_pnl": s["realized_pnl"],
        "unrealized_pnl": s["unrealized_pnl"],
        "pnl_pct": pf["pnl_pct"],
        "num_positions": pf["num_open_positions"],
        "win_rate": s["win_rate"],
        "closed_trades": s["closed_trades"],
        "total_trades": s["total_trades"],
        "decisions_last_hour": dec_1h,
        "trades_last_hour": trd_1h,
        "detail": detail,
    }
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO hourly_reports
               (portfolio_name, ts, total_value, cash_balance, positions_value,
                realized_pnl, unrealized_pnl, pnl_pct, num_positions, win_rate,
                closed_trades, total_trades, decisions_last_hour, trades_last_hour, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, rec["ts"], rec["total_value"], rec["cash_balance"],
             rec["positions_value"], rec["realized_pnl"], rec["unrealized_pnl"],
             rec["pnl_pct"], rec["num_positions"], rec["win_rate"],
             rec["closed_trades"], rec["total_trades"], rec["decisions_last_hour"],
             rec["trades_last_hour"], rec["detail"]),
        )
        conn.commit()
    finally:
        conn.close()
    return rec


def get_hourly_reports(name: str = "default", limit: int = 168) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT * FROM hourly_reports WHERE portfolio_name = ?
               ORDER BY id DESC LIMIT ?""",
            (name, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
