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
import auth
import engine

AUTOSTART = os.environ.get("AGENT_AUTOSTART", "1") not in ("0", "false", "")


def _ensure_demo_bots() -> None:
    """No accounts yet: make sure the plain-named demo set exists (the first
    admin account claims it later, history intact)."""
    for name in engine.PROFILES:
        if engine.portfolio_exists(name):
            continue
        bal = engine.start_balance_for(name)
        engine.init_portfolio(bal, name,
                              engine._risk_for(engine.get_settings(name)))
        engine.save_settings(name, {})
        print(f"Initialized demo bot '{name}' with ${bal:,.2f}")


def main():
    engine.migrate_to_per_user_bots()
    if not auth.any_users_exist():
        _ensure_demo_bots()
        # Demo mode only: default the demo set to running unless the
        # dashboard explicitly left it paused. Users' bots are never
        # auto-started — each owner presses Start on their own copies.
        if AUTOSTART:
            for name in engine.PROFILES:
                if engine.get_agent_state(name)["status"] != "paused":
                    agent.start(name)

    names = engine.active_bot_names()
    print(f"Worker live; supervising {len(names)} bot portfolios")
    agent.supervise_forever()  # blocks forever


if __name__ == "__main__":
    main()
