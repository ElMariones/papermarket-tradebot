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
            """
        )
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
    "take_profit_pct": 0.20,       # close when price up 20% vs entry
    "stop_loss_pct": 0.25,         # close when price down 25% vs entry
}


def get_settings(name: str = "default") -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT config FROM agent_settings WHERE portfolio_name = ?", (name,)
        ).fetchone()
        if row:
            cfg = {**DEFAULT_SETTINGS, **json.loads(row["config"])}
            return cfg
        return dict(DEFAULT_SETTINGS)
    finally:
        conn.close()


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

def add_funds(amount: float, name: str = "default") -> dict:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pf = pe._active_portfolio(conn, name)
        new_balance = pf["cash_balance"] + amount
        # Treat a deposit as added capital: raise the cost basis (starting_balance)
        # so P&L stays honest (deposits aren't profit).
        new_start = pf["starting_balance"] + amount
        new_peak = pf["peak_value"] + amount
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


def settle_position(token_id: str, side: str, price: float,
                    name: str = "default", reasoning: str = "") -> dict:
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
                shares, price, fee, total_cost, reasoning, executed_at, entry_avg)
               VALUES (?, ?, ?, ?, 'SELL', ?, ?, 0, ?, ?, ?, ?)""",
            (pid, token_id, pos["market_question"], side, round(shares, 4),
             round(price, 6), round(proceeds, 4), reasoning, now, pos["avg_entry"]),
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
        balance = float(os.environ.get("TRADEBOT_START_BALANCE", "200"))
    if balance <= 0:
        raise ValueError("Reset balance must be positive")

    # Preserve the prior settings (so a raised position cap survives reset).
    prior_settings = get_settings(name)
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
    init_portfolio(balance, name, risk)
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
