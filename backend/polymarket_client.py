"""
polymarket_client.py — live market discovery via the Gamma API.

Read-only. No auth required for public market data, which is all paper
trading needs. The engine (engine.py) handles per-token order books and
prices via the CLOB API; this module handles the "what markets exist"
discovery the strategy scans over.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from engine import GAMMA_API, api_get


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_market(m: dict) -> dict | None:
    """Normalize a raw Gamma market into the shape the strategy expects."""
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(token_ids) < 2 or len(prices) < 2:
        return None

    return {
        "id": m.get("id"),
        "question": m.get("question", "Unknown market"),
        "slug": m.get("slug"),
        "yes_token": token_ids[0],
        "no_token": token_ids[1],
        "yes_price": _f(prices[0]),
        "no_price": _f(prices[1]),
        "outcomes": outcomes,
        "volume24hr": _f(m.get("volume24hr")),
        "volume": _f(m.get("volume")),
        "liquidity": _f(m.get("liquidity")),
        "end_date": m.get("endDate"),
        "closed": bool(m.get("closed")),
        "active": bool(m.get("active")),
        "resolution_status": m.get("umaResolutionStatus"),
    }


def fetch_active_markets(limit: int = 40, order: str = "volume24hr") -> list[dict]:
    """
    Fetch the top active, open markets by a given ordering (default 24h volume).
    Returns normalized market dicts.
    """
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": order,
        "ascending": "false",
    }
    raw = api_get(f"{GAMMA_API}/markets?{urlencode(params)}")
    out = []
    for m in raw if isinstance(raw, list) else []:
        parsed = _parse_market(m)
        if parsed:
            out.append(parsed)
    return out


def fetch_market_by_token(token_id: str) -> dict | None:
    """Look up the resolution/closed status for a market by one of its tokens."""
    raw = api_get(f"{GAMMA_API}/markets?clob_token_ids={token_id}&limit=1")
    if isinstance(raw, list) and raw:
        return _parse_market(raw[0]) or {"raw": raw[0]}
    return None


if __name__ == "__main__":
    # Quick smoke test: pull 5 live markets and print their prices.
    print("Fetching 5 live Polymarket markets...\n")
    for i, mk in enumerate(fetch_active_markets(limit=5), 1):
        print(f"{i}. {mk['question'][:65]}")
        print(f"   YES {mk['yes_price']:.3f} | NO {mk['no_price']:.3f} | "
              f"vol24h ${mk['volume24hr']:,.0f} | ends {mk['end_date']}")
        print(f"   yes_token={mk['yes_token'][:18]}...\n")
