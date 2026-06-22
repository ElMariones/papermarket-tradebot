"""
strategy.py — the decision engine.

Strategy: **favorite-longshot bias exploitation** with a liquidity filter.

Prediction markets exhibit a well-documented inefficiency: longshots
(low-probability outcomes) are systematically *overpriced*, while clear
favorites (high-probability outcomes) are slightly *underpriced*. We harvest
both sides of that bias:

  * BUY YES on favorites priced in [fav_low, fav_high]  — favorite is underpriced
  * BUY NO  on extreme longshots (YES price < longshot_thresh) — longshot
    is overpriced, so the complementary NO is underpriced

Every candidate must clear liquidity gates (24h volume, resting book depth,
bid/ask spread) before we act. Every decision — act OR pass — is explained
in plain English and logged.

This module is pure logic: it reads live market data and returns a decision.
It never touches the ledger; the agent loop executes whatever it returns.
"""

from __future__ import annotations

import re

from engine import fetch_orderbook


# --------------------------------------------------------------------------
# Market classification — used by the optional `exclude_categories` filter.
#
# Log analysis (logs/, 4 profiles, 2026-06-22) showed the entire net loss came
# from single-match and live in-play sports markets, while markets that resolve
# by clean convergence (elections, multi-day "out by" markets, tournament
# outrights) were profitable:
#
#   inplay (halftime/period)        -38.70   4 trades    0% win   <- worst
#   single-match moneyline          -33.24  21 trades   57% win
#   single-match spread/totals/draw  -6.43  73 trades   77% win
#   politics / macro                +29.66  17 trades  100% win
#   tournament outright (golf)      +10.63   4 trades  100% win
#
# The favorite-longshot edge is real but needs the price to *converge* to the
# resolved value. A single soccer/tennis match (esp. live) is high-variance:
# a 0.85 favorite can crater to 0.02 on one goal, blowing through the stop.
# These two structural tags let a profile opt out of that whole class. The tags
# are sport-agnostic, so they also catch the in-play tennis loss from the
# earlier run (the Sabalenka book-404 episode in TRADING_ANALYSIS.md).
# --------------------------------------------------------------------------

_INPLAY_MARKERS = (
    "leading at halftime", "at halftime", "1st half", "2nd half", "first half",
    "second half", "at the half", "halftime", "leading after", "in-game",
)
_EVENT_STRUCT = (
    "spread:", ": o/u", " o/u ", "o/u ", "both teams to score",
    "end in a draw", "to draw", "clean sheet",
)
_DAILY_MONEYLINE = re.compile(r"win on \d{4}-\d{2}-\d{2}")


def classify_market(question: str) -> set[str]:
    """Tag a market by structure. Returns any of {"inplay", "single_match"}.

    A profile can list either tag in its `exclude_categories` setting to skip
    that whole class of market. Markets with no tag (elections, "out by DATE",
    tournament outrights, price markets) are left untouched.
    """
    s = (question or "").lower()
    tags: set[str] = set()
    if any(k in s for k in _INPLAY_MARKERS):
        tags.add("inplay")
    if (any(k in s for k in _EVENT_STRUCT)
            or _DAILY_MONEYLINE.search(s)
            or " vs. " in s or " vs " in s):
        tags.add("single_match")
    return tags


def _book_depth_usd(levels: list[dict]) -> float:
    """Total USD resting across order-book levels (price * size summed)."""
    total = 0.0
    for lv in levels:
        try:
            total += float(lv["price"]) * float(lv["size"])
        except (TypeError, ValueError, KeyError):
            continue
    return total


def _best(levels: list[dict], side: str) -> float | None:
    """Best price: highest bid or lowest ask. CLOB returns book sorted with
    best price last for asks-ascending/bids-ascending, so scan explicitly."""
    prices = []
    for lv in levels:
        try:
            prices.append(float(lv["price"]))
        except (TypeError, ValueError, KeyError):
            continue
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def evaluate_market(market: dict, settings: dict, portfolio: dict) -> dict:
    """
    Decide what to do about one market.

    Returns a decision dict:
      {signal, side, token_id, confidence, size_usd, reasoning, market_question}
    where signal in {BUY_YES, BUY_NO, PASS}.
    """
    q = market["question"]
    yes_price = market["yes_price"]
    no_price = market["no_price"]
    vol24 = market["volume24hr"]

    def decision(signal, side=None, token=None, conf=0.0, size=0.0, reason=""):
        return {
            "signal": signal, "side": side, "token_id": token,
            "confidence": round(conf, 3), "size_usd": round(size, 2),
            "reasoning": reason, "market_question": q,
        }

    # --- skip closed / degenerate markets ---
    if market.get("closed") or yes_price <= 0 or yes_price >= 1:
        return decision("PASS", reason=f"Closed or degenerate pricing (YES={yes_price}).")

    # --- category gate: opt out of money-losing market classes (per profile) ---
    excluded = set(settings.get("exclude_categories") or [])
    if excluded:
        hit = classify_market(q) & excluded
        if hit:
            return decision("PASS", reason=(
                f"Excluded market class {sorted(hit)} — this profile does not "
                f"trade single-match/in-play sports (high variance, net loss in "
                f"log analysis)."))

    # --- liquidity gate 1: 24h volume ---
    if vol24 < settings["min_volume24h"]:
        return decision("PASS", reason=(
            f"Volume too thin: 24h ${vol24:,.0f} < "
            f"${settings['min_volume24h']:,.0f} floor."))

    # --- classify the signal: buy the FAVORITE side when it's in the band ---
    # The favorite is whichever outcome is priced higher. Favorite-longshot bias
    # says that side is underpriced; its complement (the longshot) is overpriced.
    # Buying the favorite IS fading the longshot — one symmetric rule covers both
    # a YES-favorite and the common "Will X win?" markets where NO is the favorite.
    fav_low, fav_high = settings["fav_low"], settings["fav_high"]

    if yes_price >= no_price:
        fav_side, fav_token, fav_price = "YES", market["yes_token"], yes_price
    else:
        fav_side, fav_token, fav_price = "NO", market["no_token"], no_price

    if not (fav_low <= fav_price <= fav_high):
        return decision("PASS", reason=(
            f"Favorite is {fav_side} at {fav_price:.2f}, outside the underpriced "
            f"band [{fav_low:.2f},{fav_high:.2f}] "
            f"({'too close to certainty' if fav_price > fav_high else 'no clear favorite'})."))

    side, token = fav_side, fav_token
    # Confidence/sizing is driven by UPSIDE ROOM, not by how close the favorite
    # is to certainty. A favorite at fav_low still has room to converge to 1.0;
    # one at fav_high has almost none. The old model did the opposite — it bet
    # biggest on 0.96 favorites with ~4% upside against a much larger stop, an
    # inverted reward/risk (see TRADING_ANALYSIS.md). EV per dollar of the
    # favorite-longshot premium is highest where the entry is cheapest in-band.
    upside_room = (fav_high - fav_price) / max(fav_high - fav_low, 1e-9)
    base = 0.60 + 0.25 * _clamp(upside_room)
    thesis = (f"{fav_side} is the favorite at {fav_price:.2f}, inside the "
              f"underpriced-favorite band [{fav_low:.2f},{fav_high:.2f}]; "
              f"favorite-longshot bias implies the favorite is underpriced "
              f"(equivalently, the {('NO' if fav_side=='YES' else 'YES')} longshot "
              f"is overpriced).")

    # --- price gate: don't pay near-certainty prices (no upside left) ---
    entry_px = no_price if side == "NO" else yes_price
    if entry_px > settings.get("max_entry_price", 0.97):
        return decision("PASS", side, token, base, 0.0, (
            f"{thesis} But {side} entry {entry_px:.3f} exceeds max entry "
            f"{settings.get('max_entry_price', 0.97):.2f} — too little upside."))

    # --- liquidity gate 2 & 3: real order book depth + spread ---
    try:
        book = fetch_orderbook(token)
    except Exception as exc:
        return decision("PASS", reason=f"Could not load order book ({exc}).")

    asks, bids = book.get("asks", []), book.get("bids", [])
    if not asks or not bids:
        return decision("PASS", reason="One-sided/empty order book — illiquid.")

    best_ask, best_bid = _best(asks, "ask"), _best(bids, "bid")
    if best_ask is None or best_bid is None:
        return decision("PASS", reason="Unreadable order book.")
    spread = best_ask - best_bid
    if spread > settings["max_spread"]:
        return decision("PASS", reason=(
            f"Spread too wide: {spread:.3f} > {settings['max_spread']:.3f} "
            f"(bid {best_bid:.3f}/ask {best_ask:.3f})."))

    ask_depth = _book_depth_usd(asks)
    if ask_depth < settings["min_book_usd"]:
        return decision("PASS", reason=(
            f"Thin book: only ${ask_depth:,.0f} resting on asks < "
            f"${settings['min_book_usd']:,.0f}."))

    # spread quality bonus: tighter spread -> higher confidence
    spread_bonus = 0.10 * _clamp((settings["max_spread"] - spread) / settings["max_spread"])
    confidence = _clamp(base + spread_bonus)

    if confidence < settings["confidence_threshold"]:
        return decision(side and f"BUY_{side}", side, token, confidence, 0.0, (
            f"{thesis} Confidence {confidence:.2f} < threshold "
            f"{settings['confidence_threshold']:.2f}; passing."))

    # --- position sizing: risk_per_trade_pct of total value, scaled by confidence ---
    total_value = portfolio["total_value"]
    max_risk_usd = settings["risk_per_trade_pct"] * total_value
    size = max(settings["min_trade_usd"], max_risk_usd * confidence)
    size = min(size, max_risk_usd, portfolio["cash_balance"])

    if size < settings["min_trade_usd"] or size > portfolio["cash_balance"]:
        return decision("PASS", side, token, confidence, 0.0, (
            f"{thesis} But sizing ${size:.2f} is below the ${settings['min_trade_usd']:.2f} "
            f"minimum or exceeds cash ${portfolio['cash_balance']:.2f}."))

    reason = (
        f"{thesis} Book OK (spread {spread:.3f}, asks depth ${ask_depth:,.0f}). "
        f"Confidence {confidence:.2f}. Sizing ${size:.2f} "
        f"({settings['risk_per_trade_pct']*100:.0f}% risk cap x confidence) into {side}.")
    return decision(f"BUY_{side}", side, token, confidence, size, reason)
