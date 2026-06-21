"""
agent.py — the autonomous trading loop.

One cycle:
  1. Manage open positions: settle resolved markets, take profit / stop loss.
  2. Scan the top live markets, run the strategy, execute paper buys that
     pass both the strategy's confidence bar and the engine's risk guards.
  3. Record an equity snapshot for the curve.

The loop runs in a background thread driven by `run_forever`, gated by the
agent control state (running / paused / stopped) so the dashboard can
start/stop/pause it. All execution is SIMULATED against REAL live prices.
"""

from __future__ import annotations

import os
import threading
import time
import traceback

import engine
import strategy
from polymarket_client import fetch_active_markets, fetch_market_by_token

# Hard safety guard. Flipping this to True is NOT enough to trade real money —
# there is no live-execution code path in this project at all. It exists so the
# intent is explicit and a future live build must add a deliberate code path.
LIVE_TRADING = False


def _resolved_outcome(market: dict, side: str) -> float | None:
    """If the market has resolved, return the settle price for `side` (1.0/0.0)."""
    if not market:
        return None
    if not market.get("closed"):
        return None
    yes_p = market.get("yes_price", 0.5)
    # After resolution Gamma reports outcomePrices as ~1/0.
    yes_won = yes_p >= 0.5
    if side == "YES":
        return 1.0 if yes_won else 0.0
    return 0.0 if yes_won else 1.0


def _exit_pnl(r) -> float:
    return r["realized_pnl"] if isinstance(r, dict) else r[0]["realized_pnl"]


def _close_or_settle(token: str, side: str, name: str, cur: float, reason: str):
    """
    Exit a position via the live order book, but fall back to a no-book
    settlement when the book is gone (HTTP 404 / no bids) — which is exactly
    what happens the instant a market resolves. Without this fallback a resolved
    position can never be closed and gets stuck forever (the historical
    "SL close failed ... 404 Not Found" bug that froze capital). On fallback we
    settle at the decided outcome: 1.0 if our side is winning, else 0.0.
    """
    try:
        return engine.close_position(token, side, name, reasoning=reason)
    except Exception:
        settle_px = 1.0 if cur >= 0.5 else 0.0
        return engine.settle_position(
            token, side, settle_px, name,
            reasoning=f"{reason} [order book gone — settled at outcome {settle_px:.0f}]")


def manage_positions(name: str, settings: dict) -> list[str]:
    """Settle resolved markets and apply take-profit / stop-loss exits."""
    notes = []
    resolve_hi = settings.get("resolve_hi", 0.99)
    resolve_lo = settings.get("resolve_lo", 0.01)
    sl_price = settings.get("stop_loss_price", 0.50)
    pf = engine.get_portfolio(name, refresh_prices=True)
    for pos in pf["positions"]:
        token, side = pos["token_id"], pos["side"]
        entry, cur = pos["avg_entry"], pos["current_price"]
        q = (pos.get("market_question") or "")[:50]

        # 1) resolution settlement — Gamma 'closed' flag, when available.
        market = None
        try:
            market = fetch_market_by_token(token)
        except Exception:
            pass
        settle = _resolved_outcome(market, side)

        # 1b) price-based resolution: Gamma's 'closed' flag lags the market by
        # minutes, but the CLOB price pins to ~1.0/0.0 (and the book 404s) the
        # moment it resolves. Treat an extreme mark as resolved so winners get
        # booked and losers stop spamming failed closes — both free the slot.
        if settle is None and cur is not None:
            if cur >= resolve_hi:
                settle = 1.0
            elif cur <= resolve_lo:
                settle = 0.0
        if settle is not None:
            try:
                r = engine.settle_position(
                    token, side, settle, name,
                    reasoning=f"Resolved; settled {side} at {settle:.0f}.")
                engine.log_decision(name, token, q, "EXIT", True, None,
                                    f"RESOLVED -> settled {side} @ {settle:.0f}, "
                                    f"P&L ${r['realized_pnl']:+.2f}")
                notes.append(f"settled {side} '{q}' @ {settle:.0f} "
                             f"(P&L ${r['realized_pnl']:+.2f})")
            except Exception as exc:
                notes.append(f"settle failed {q}: {exc}")
            continue

        if entry <= 0:
            continue
        change = (cur - entry) / entry

        # 2) take profit — recycle capital once a position has run far enough.
        if change >= settings["take_profit_pct"]:
            try:
                r = _close_or_settle(token, side, name, cur,
                                     f"Take profit: +{change*100:.1f}% vs entry.")
                pnl = _exit_pnl(r)
                engine.log_decision(name, token, q, "EXIT", True, None,
                                    f"TAKE PROFIT +{change*100:.1f}% -> P&L ${pnl:+.2f}")
                notes.append(f"took profit {side} '{q}' +{change*100:.1f}% "
                             f"(P&L ${pnl:+.2f})")
            except Exception as exc:
                notes.append(f"TP close failed {q}: {exc}")
            continue

        # 3) stop loss — thesis broken. Fire on EITHER a wide % drawdown OR an
        # absolute price floor (our 'favorite' is no longer favored). The price
        # floor caps catastrophic tails earlier than the % stop on cheap entries.
        if change <= -settings["stop_loss_pct"] or cur <= sl_price:
            why = (f"Stop loss: {change*100:.1f}% vs entry"
                   if change <= -settings["stop_loss_pct"]
                   else f"Stop: price {cur:.2f} below floor {sl_price:.2f} (thesis broken)")
            try:
                r = _close_or_settle(token, side, name, cur, why + ".")
                pnl = _exit_pnl(r)
                engine.log_decision(name, token, q, "EXIT", True, None,
                                    f"{why} -> P&L ${pnl:+.2f}")
                notes.append(f"stopped out {side} '{q}' @ {cur:.2f} "
                             f"(P&L ${pnl:+.2f})")
            except Exception as exc:
                notes.append(f"SL close failed {q}: {exc}")
    return notes


def _cooldown_tokens(name: str, settings: dict) -> set[str]:
    """Tokens we exited within the cooldown window — don't immediately re-enter.

    The historical logs show the bot stopping out of a market and re-buying the
    SAME losing market four seconds later, doubling the loss. A cooldown blocks
    that churn (and re-buying a market that just resolved)."""
    minutes = settings.get("reentry_cooldown_min", 180)
    if minutes <= 0:
        return set()
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    out = set()
    try:
        for t in engine.get_trades(name, 500):
            if t["action"] == "SELL" and (t.get("executed_at") or "") >= cutoff:
                out.add(t["token_id"])
    except Exception:
        pass
    return out


def scan_and_trade(name: str, settings: dict) -> list[str]:
    """Scan live markets, run strategy, execute qualifying buys."""
    notes = []
    pf = engine.get_portfolio(name, refresh_prices=False)
    open_tokens = {p["token_id"] for p in pf["positions"]}
    cooldown = _cooldown_tokens(name, settings)

    try:
        markets = fetch_active_markets(limit=int(settings["markets_per_scan"]))
    except Exception as exc:
        return [f"market scan failed: {exc}"]

    for mk in markets:
        pf = engine.get_portfolio(name, refresh_prices=False)
        if pf["num_open_positions"] >= settings["max_concurrent_positions"]:
            notes.append("max concurrent positions reached; stopping scan.")
            break
        if mk["yes_token"] in open_tokens or mk["no_token"] in open_tokens:
            continue  # already holding this market
        if mk["yes_token"] in cooldown or mk["no_token"] in cooldown:
            continue  # recently exited — in cooldown to avoid churn/re-entry

        dec = strategy.evaluate_market(mk, settings, pf)
        acted = False
        if dec["signal"] in ("BUY_YES", "BUY_NO") and dec["size_usd"] > 0:
            try:
                res = engine.place_order(
                    token_id=dec["token_id"], side=dec["side"],
                    size=dec["size_usd"], reasoning=dec["reasoning"],
                    portfolio_name=name)
                acted = True
                open_tokens.add(dec["token_id"])
                notes.append(f"BUY {dec['side']} ${dec['size_usd']:.2f} "
                             f"'{mk['question'][:45]}' @ {res['avg_price']:.3f}")
            except Exception as exc:
                # risk guard or balance rejection — record why
                dec["reasoning"] += f" [REJECTED: {exc}]"
        # Only persist decisions worth reviewing: trades, rejected buy signals,
        # and genuine in-band candidates (confidence > 0). Skip the ~90% of
        # trivial "outside band / thin volume" PASSes — logging one row per
        # market per cycle for 4 profiles bloated the DB by thousands of rows
        # an hour (and the page cache that came with it).
        if acted or dec["signal"] != "PASS" or (dec["confidence"] or 0) > 0:
            engine.log_decision(name, dec["token_id"], dec["market_question"],
                                dec["signal"], acted, dec["confidence"], dec["reasoning"])
    return notes


def run_cycle(name: str = "default") -> dict:
    """Run exactly one full agent cycle and return a summary."""
    settings = engine.get_settings(name)
    exit_notes = manage_positions(name, settings)
    entry_notes = scan_and_trade(name, settings)
    state = engine.record_equity(name)
    summary = exit_notes + entry_notes
    msg = "; ".join(summary) if summary else "no action (no qualifying signals)"
    engine.mark_cycle(name, msg[:500])
    return {
        "total_value": state["total_value"],
        "cash": state["cash_balance"],
        "pnl": state["pnl"],
        "open_positions": state["num_open_positions"],
        "actions": summary,
        "message": msg,
    }


# --------------------------------------------------------------------------
# Control = pure DB state, so it works across processes (web vs worker
# machine). The web process's start/pause/stop buttons just flip this flag;
# whichever runner is executing cycles (an in-process thread locally, or the
# separate worker on Fly) obeys it via the shared database.
# --------------------------------------------------------------------------

def start(name: str = "default") -> dict:
    return engine.set_agent_status(name, "running", message="agent started")


def pause(name: str = "default") -> dict:
    return engine.set_agent_status(name, "paused", message="agent paused")


def resume(name: str = "default") -> dict:
    return start(name)


def stop(name: str = "default") -> dict:
    return engine.set_agent_status(name, "stopped", message="agent stopped")


HOURLY_REPORT_SEC = int(os.environ.get("TRADEBOT_REPORT_INTERVAL_SEC", "3600"))


def _emit_hourly_report(name: str):
    """Persist + print an hourly report (stdout shows up in `fly logs`)."""
    try:
        r = engine.record_hourly_report(name)
        print(
            f"[HOURLY {r['ts'][:19]}] total=${r['total_value']:.2f} "
            f"({r['pnl_pct']:+.2f}%) cash=${r['cash_balance']:.2f} "
            f"pos={r['num_positions']} realized=${r['realized_pnl']:+.2f} "
            f"unrealized=${r['unrealized_pnl']:+.2f} winrate={r['win_rate']:.0f}% "
            f"trades_1h={r['trades_last_hour']} decisions_1h={r['decisions_last_hour']}",
            flush=True)
    except Exception as exc:
        print(f"[HOURLY] report failed: {exc}", flush=True)


def run_forever(name: str = "default", stop_event: threading.Event | None = None,
                start_delay: float = 0.0):
    """
    Long-lived runner: execute cycles while control state is 'running',
    idle while 'paused', exit-loop check honored each tick. Used by both the
    standalone worker process and the local in-process worker thread.
    Emits a persisted performance report every HOURLY_REPORT_SEC.

    `start_delay` staggers multiple profile workers so their network-heavy
    scans don't all spike memory at the same instant (avoids boot-time OOM).
    """
    if start_delay:
        for _ in range(int(start_delay)):
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(1)
    last_report = time.monotonic()
    _emit_hourly_report(name)  # baseline at startup
    while stop_event is None or not stop_event.is_set():
        st = engine.get_agent_state(name)
        if st["status"] == "running":
            try:
                run_cycle(name)
            except Exception:
                engine.set_agent_status(
                    name, "running",
                    message=f"cycle error: {traceback.format_exc()[-300:]}")
            interval = int(engine.get_settings(name)["scan_interval_sec"])
        else:
            interval = 2  # paused/stopped: poll the control flag cheaply
        if time.monotonic() - last_report >= HOURLY_REPORT_SEC:
            _emit_hourly_report(name)
            last_report = time.monotonic()
        # sleep in 1s slices so control changes are picked up quickly
        for _ in range(max(1, interval)):
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(1)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="TradeBOT agent")
    ap.add_argument("--name", default="default")
    ap.add_argument("--cycles", type=int, default=1, help="run N cycles then exit")
    ap.add_argument("--interval", type=int, default=0, help="seconds between cycles")
    args = ap.parse_args()
    for i in range(args.cycles):
        print(f"\n=== CYCLE {i+1}/{args.cycles} ===")
        out = run_cycle(args.name)
        print(json.dumps(out, indent=2))
        if i < args.cycles - 1 and args.interval:
            time.sleep(args.interval)
