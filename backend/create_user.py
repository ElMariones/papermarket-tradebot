#!/usr/bin/env python3
"""
create_user.py — the only way accounts get made (no signup form on the site).

    python3 backend/create_user.py <username> <password> [--role admin]
    python3 backend/create_user.py <username> <password> --balance 500

Creates the account AND its own set of the four bots (Kaladin, Adolin,
Dalinar, Renarin) in one step — each bot with its own money (default $200,
or --balance per bot), its preset strategy (editable per copy in the
dashboard), created STOPPED so nothing trades until the owner presses Start.

The FIRST admin account additionally claims the original unowned demo set,
history and all, instead of getting fresh copies of those bots.

Uses the same DB as the dashboard (TRADEBOT_DB_PATH or
~/.polymarket-paper/portfolio.db), so run it wherever the server's database
lives.
"""

from __future__ import annotations

import argparse
import sys

import auth
import engine


def main():
    ap = argparse.ArgumentParser(description="Create a TradeBOT account")
    ap.add_argument("username")
    ap.add_argument("password")
    ap.add_argument("--role", choices=["user", "admin"], default="user")
    ap.add_argument("--balance", type=float, default=None,
                    help="starting paper balance PER BOT (default $200 or "
                         "TRADEBOT_START_BALANCE)")
    args = ap.parse_args()

    try:
        user = auth.create_user(args.username, args.password, args.role)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        claimed = []
        if user["role"] == "admin":
            # the original demo set (with all its history) becomes this
            # admin's — only fires while an unowned plain-named set exists
            claimed = engine.claim_legacy_bots(user["id"], user["username"])
            for name in claimed:
                print(f"Claimed existing bot (history kept): {name}")
        made = engine.create_user_bots(user["username"], user["id"],
                                       args.balance)
        for name in made:
            print(f"Created {name} (${engine.start_balance_for(name) if args.balance is None else args.balance:,.2f}, stopped)")
        total = len(claimed) + len(made)
        print(f"Done: {user['role']} '{user['username']}' with {total} bots. "
              f"New bots start stopped — press Start in the dashboard.")
    except Exception as exc:
        print(f"Created {user['role']} '{user['username']}', but bot setup "
              f"failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
