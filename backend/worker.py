"""
worker.py — standalone agent loop entrypoint.

Runs the trading cycle forever, driven by the control state in the shared
database (so the dashboard's start/pause/stop still works cross-process).

Used when you want the agent as its own long-lived process — e.g. a separate
Fly `worker` machine, or a second local terminal. The web server can also run
this loop in-process (TRADEBOT_STANDALONE=1), which is the simpler default.

All market data REAL · all execution SIMULATED · LIVE_TRADING is False.
"""

from __future__ import annotations

import os

import agent
import engine

PORTFOLIO = os.environ.get("TRADEBOT_PORTFOLIO", "default")
STARTING_BALANCE = float(os.environ.get("TRADEBOT_START_BALANCE", "200"))
AUTOSTART = os.environ.get("AGENT_AUTOSTART", "1") not in ("0", "false", "")


def main():
    if not engine.portfolio_exists(PORTFOLIO):
        engine.init_portfolio(STARTING_BALANCE, PORTFOLIO, {
            "max_position_pct": 0.05, "max_concurrent_positions": 10,
            "max_single_market_pct": 0.10, "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.30, "human_approval_pct": 0.20,
        })
        print(f"Initialized '{PORTFOLIO}' portfolio with ${STARTING_BALANCE:,.2f}")

    # On boot, default to running for 24/7 operation unless the dashboard
    # explicitly left it paused (or autostart is disabled).
    state = engine.get_agent_state(PORTFOLIO)
    if AUTOSTART and state["status"] != "paused":
        agent.start(PORTFOLIO)

    print(f"Worker live for portfolio '{PORTFOLIO}'. "
          f"Status: {engine.get_agent_state(PORTFOLIO)['status']}. "
          f"Scan interval: {engine.get_settings(PORTFOLIO)['scan_interval_sec']}s")
    agent.run_forever(PORTFOLIO)


if __name__ == "__main__":
    main()
