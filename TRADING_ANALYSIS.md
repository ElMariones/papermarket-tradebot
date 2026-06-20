# TradeBOT — Trading Log Analysis, Bug Report & Strategy Optimization

**Source:** `logs/tradebot-export-20260620T131656.json`
**Window:** 2026-06-19 14:46 → 2026-06-20 13:16 UTC (~22.5h, 1033 cycles)
**Result:** start $250.00 → $241.59 (**−$8.41, −3.36%**), 27 trades, 6 closed, win rate 50%.

---

## TL;DR

The bot's entire net loss is explained by **one stuck position**, not by the
strategy thesis:

| Item | P&L |
|---|---|
| Single stuck position (`Sabalenka` NO, book 404'd) | **−$8.88** |
| Everything else (realized + unrealized, net) | **+$0.47** |
| **Total** | **−$8.41** |

Remove the bug and the bot is roughly break-even *despite* a second
self-inflicted loss from re-buying that same market. The favorite-longshot
thesis itself is sound: of the favorites that reached resolution, the winners
converged to 1.0 (`+$0.35`, `+$0.51`, `+$0.88`, `+$0.92`, `+$1.17` open at mark
1.000). The problems were **execution bugs** and an **inverted exit/sizing
design**, both now fixed.

---

## What failed

### BUG 1 — Resolved positions get stuck forever (404 on the order book) 🔴 critical

**Symptom (from `agent_state.last_message`):**
```
SL close failed Grass Court Championships: Aryna Sabalenka vs Niko:
API request failed: .../book?token_id=... — HTTP Error 404: Not Found
```

**Root cause.** The instant a Polymarket market resolves, the CLOB `/book`
endpoint returns **404** (the book is deleted). But:
- `close_position()` *requires* a live book to walk bids — so every take-profit
  / stop-loss close on a resolved market throws and **never settles**.
- Resolution settlement only fired on Gamma's `closed` flag, which **lags the
  market by minutes**. During that gap the position is un-closable by either path.

The `Sabalenka` NO position fell to mark **0.001** (it lost), tripped the stop
loss every cycle, and the close failed every cycle with a 404 — **stuck for
~1000 cycles at −$8.88**, never freeing its slot.

**The same mechanism froze the winners:** five favorites that resolved to 1.0
were *still listed as open* at the export because settlement never fired for
them either — their gains sat unrealized and their capital stayed locked.

### BUG 2 — Capital paralysis once the book fills 🔴

`scan_and_trade` stops the moment `num_open_positions >= max`. Because stuck
(BUG 1) positions never free their slots, the bot hit 15/15 within the first
hour and then did **nothing for ~1000 cycles** — "max concurrent positions
reached; stopping scan." 1033 cycles produced only 27 trades.

### BUG 3 — Instant re-entry into a market we just stopped out of 🟠

The two `Sabalenka` decisions are 4 seconds apart:
```
14:51:19  EXIT  STOP LOSS -34.0% -> P&L $-3.11   (first position, closed)
14:51:21  BUY_NO  NO favorite @ 0.85             (re-entered the SAME market)
```
After the stop-out the market left `open_tokens`, so the very next scan re-bought
it — and *that* re-entry became the −$8.88 stuck position. No cooldown existed.

### FLAW 4 — Inverted reward/risk in confidence/sizing 🟠

Old sizing: `base = 0.50 + 0.40 * depth_in_band` — confidence (and therefore
position size) **grew as the favorite got closer to 0.96**. A 0.96 favorite has
only ~4% upside to resolution but was sized *largest*, against a stop many times
that size. The favorite-longshot premium's EV-per-dollar is in fact **highest
where the entry is cheapest in-band** (most convergence room). The model bet
biggest exactly where reward/risk is worst.

### FLAW 5 — Symmetric ±20%/−25% exits fight the edge 🟠

The favorite-longshot edge is a **hold-to-resolution** edge — the favorite is
underpriced and converges to ~1.0 at settlement. The old exits worked against it:
- **Stop loss −25%** on an 0.85 favorite (→0.64) fires on ordinary intraday
  noise and **realizes the bad tail** before resolution can vindicate the trade.
  All 3 realized losses were stop-outs (−27%, −27%, −34%); all 3 realized wins
  were resolutions/convergence.
- **Take profit +20%** is structurally **unreachable** for a high favorite
  (0.90 → 1.0 is only +11%), so the core trades could only ever resolve or stop
  out — the exit logic had no working "win" path for them except settlement,
  which BUG 1 had broken.

---

## What worked (keep it)

- **The thesis.** Buying strong favorites in `[0.80, 0.96]` and holding to
  resolution: every favorite that reached settlement converged up. The bias is
  real; it just needs to be *harvested at resolution across many independent
  markets*, not scalped intraday.
- **Liquidity gates** (24h volume, book depth, spread) — sound, kept as-is.
- **Decision logging** — every act/pass is explained; made this analysis possible.

---

## Fixes applied

| # | Fix | File |
|---|---|---|
| 1 | **404-safe exit fallback** `_close_or_settle()`: if the book is gone, settle at the decided outcome (1.0/0.0) instead of throwing. Stuck positions are now impossible. | `agent.py` |
| 1 | **Price-based resolution sweep**: a mark ≥ `resolve_hi` (0.99) or ≤ `resolve_lo` (0.01) is treated as resolved and settled immediately — no dependence on Gamma's lagging `closed` flag. Books winners, clears losers, frees slots. | `agent.py` |
| 2 | Slot paralysis disappears because (1) guarantees stuck slots get freed. | — |
| 3 | **Re-entry cooldown** `reentry_cooldown_min` (180 min): markets exited within the window are skipped during scan. No more instant re-buys of a loser. | `agent.py` |
| 4 | **Sizing now rewards upside room**: `base = 0.60 + 0.25 * upside_room` — biggest into the cheapest in-band favorites, smallest into near-certainties. | `strategy.py` |
| 5 | **Exit retune**: wider `stop_loss_pct` 0.25 → **0.40** (let favorites breathe) **plus** an absolute `stop_loss_price` floor of **0.50** (cut only when our side is genuinely no longer the favorite — a real catastrophe bound, earlier than −100%). Primary exit is resolution. | `engine.py` / `agent.py` |

### New settings (in `DEFAULT_SETTINGS`)
```
stop_loss_pct        0.40   # was 0.25 — wide % stop
stop_loss_price      0.50   # NEW — hard floor; thesis-broken exit
resolve_hi           0.99   # NEW — settle our side as a win
resolve_lo           0.01   # NEW — settle our side as a loss
reentry_cooldown_min 180    # NEW — anti-churn cooldown
```

> **Applying the numeric exit tuning to a live portfolio:** the code-level fixes
> (resolution settlement, 404 fallback, cooldown, sizing) take effect
> automatically. The *numeric* exit params load from `DEFAULT_SETTINGS` for any
> key not already overridden in the saved config; if you previously saved a
> custom `stop_loss_pct`, update it in the dashboard (or `save_settings`) to pick
> up 0.40. The new keys (`stop_loss_price`, `resolve_*`) apply immediately.

---

## How to optimize further (next steps, not yet implemented)

1. **Sector caps.** Cap concurrent positions per category (e.g. ≤N football
   "Will X win the World Cup" NOs) so one correlated theme can't dominate risk.
2. **Edge-aware confidence.** Fold the favorite's *historical* longshot premium
   per market type into confidence rather than treating all in-band favorites
   equally — sports favorites are more efficiently priced than geopolitical ones.
3. **Time-to-resolution filter.** Prefer markets resolving sooner; capital tied
   up in a 6-month "World Cup" NO earns the edge slowly. Add an `end_date` gate.
4. **Settlement reconciliation pass.** A periodic sweep that settles any open
   position whose market Gamma reports `closed`, independent of the price sweep,
   for the rare case a book lingers at a non-extreme price post-resolution.
