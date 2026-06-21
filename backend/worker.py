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
import threading

import agent
import engine

AUTOSTART = os.environ.get("AGENT_AUTOSTART", "1") not in ("0", "false", "")


def _ensure_profile(name: str) -> None:
    if engine.portfolio_exists(name):
        return
    bal = engine.start_balance_for(name)
    cfg = engine.get_settings(name)
    engine.init_portfolio(bal, name, {
        "max_position_pct": max(0.05, float(cfg["risk_per_trade_pct"])),
        "max_concurrent_positions": int(cfg["max_concurrent_positions"]),
        "max_single_market_pct": 0.10, "daily_loss_limit_pct": 0.05,
        "max_drawdown_pct": 0.30, "human_approval_pct": 0.20,
    })
    engine.save_settings(name, {})
    print(f"Initialized profile '{name}' with ${bal:,.2f}")


def main():
    for name in engine.PROFILES:
        _ensure_profile(name)
        # Default to running unless the dashboard explicitly left it paused.
        if AUTOSTART and engine.get_agent_state(name)["status"] != "paused":
            agent.start(name)

    print(f"Worker live for {len(engine.PROFILES)} profiles: "
          f"{', '.join(engine.PROFILES)}")
    threads = [threading.Thread(target=agent.run_forever, args=(name,),
                                kwargs={"start_delay": i * 8}, daemon=True)
               for i, name in enumerate(engine.PROFILES)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
