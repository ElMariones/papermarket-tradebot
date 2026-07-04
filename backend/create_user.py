#!/usr/bin/env python3
"""
create_user.py — the only way accounts get made (no signup form on the site).

    python3 backend/create_user.py <username> <password> [--role admin]
    python3 backend/create_user.py <username> <password> --balance 500

Creates the account AND its personal paper portfolio in one step, ready to
trade manually from the Markets page. Uses the same DB as the dashboard
(TRADEBOT_DB_PATH or ~/.polymarket-paper/portfolio.db), so run it wherever
the server's database lives.
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
                    help="starting paper balance (default $200 or "
                         "TRADEBOT_START_BALANCE)")
    args = ap.parse_args()

    try:
        user = auth.create_user(args.username, args.password, args.role)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Every account gets a personal manual-trading portfolio — including the
    # admin, so Mario can pit his own picks against the bots.
    try:
        pf = engine.create_user_portfolio(user["username"], user["id"],
                                          args.balance)
        print(f"Created {user['role']} '{user['username']}' with portfolio "
              f"'{pf['name']}' (${pf['starting_balance']:,.2f} paper).")
    except Exception as exc:
        print(f"Created {user['role']} '{user['username']}', but the portfolio "
              f"failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
