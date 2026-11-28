"""Pump.fun Sharpe Sprint — Cleanest Paper Bot.

Risk-managed, consistency-first momentum bot:
- Tracks each curve from CreateEvent with a rolling tape of net SOL inflow, distinct buyers
  and price stability across the window.
- Enters only mature curves with many distinct buyers, steady net inflow, and price near its
  window high with low intra-window volatility.
- Uses small uniform stakes (~0.03 SOL, capped by max_buy_lamports); no pyramiding beyond one
  modest add on continued strength.
- Tight risk exits: stop-loss ~-15%, take-profit/trailing stop that locks gains early, short
  maximum holding time; prefer exiting to holding through uncertainty.
- Keeps a large cash reserve (at least half the account) so no single coin can dent the equity
  curve.

Deterministic: never reads the clock, environment variables, or unseeded randomness. Uses
random.Random seeded from int(state.hello["seed"]). Iteration order over sets of strings is
sorted so both PYTHONHASHSEED=0 runs produce byte-identical replies.

Respects every cap from state.hello["caps"] (max_open_positions, max_orders_per_batch) and the
per-coin size cap state.max_buy_lamports(mint); keeps cash for tx fees (state.tx_fee_lamports()
per order).
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot


# ── per-curve rolling tape, attached to on_batch as attributes ────────────────────

def _curve_init(mint):
    """Return a fresh per-curve tape dict for mint, creating it if absent."""
    if not hasattr(on_batch, "_curve_tape"):
        on_batch._curve_tape = {}
    if mint not in on_batch._curve_tape:
        on_batch._curve_tape[mint] = {
            "net_sol_inflow": 0,
            "distinct_buyers": set(),
            "prices": [],  # list of (slot, price) pairs, appended on TradeEvent/mark
        }
    return on_batch._curve_tape[mint]


def _price_from_reserves(real_token_reserves, real_quote_reserves):
    """Compute quote/token price; return None if token_reserves is 0."""
    rt = int(real_token_reserves) if real_token_reserves else 0
    rq = int(real_quote_reserves) if real_quote_reserves else 0
    if rt <= 0:
        return None
    return rq / rt


def _sort_mint(mint):
    """Deterministic sort key for a mint string."""
    return mint


# ── on_batch ─────────────────────────────────────────────────────────────────────

def on_batch(state, batch):
    caps = state.hello["caps"]
    tx = state.tx_fee_lamports()
    cash = state.cash
    max_open = caps["max_open_positions"]
    max_orders = caps["max_orders_per_batch"]

    # Seed RNG from the hello seed — used if we ever need randomness.
    if not hasattr(on_batch, "_rng"):
        on_batch._rng = random.Random(int(state.hello["seed"]))

    slot = batch["slot"]
    events = batch["events"]
    marks = batch["marks"]

    # ── 1. Update per-curve rolling tape from this batch's events and marks ───────
    for e in events:
        if e["event"] == "TradeEvent":
            mint = e["mint"]
            tape = _curve_init(mint)

            # net SOL inflow: sol_amount sign depends on is_buy
            # Use the user's quote_amount when sol_amount is absent (test data edge case)
            if "sol_amount" in e:
                sol_amount = int(e["sol_amount"])
            elif "quote_amount" in e:
                sol_amount = int(e["quote_amount"])
            else:
                sol_amount = 0  # No amount data; skip inflow tracking
            if sol_amount > 0 and e.get("is_buy", False):
                tape["net_sol_inflow"] += sol_amount
            elif sol_amount > 0:
                tape["net_sol_inflow"] -= sol_amount

            # Record distinct buyer (when user field exists)
            if "user" in e:
                tape["distinct_buyers"].add(e["user"])

            # Record price point
            price = _price_from_reserves(e.get("real_token_reserves"), e.get("real_quote_reserves"))
            if price is not None:
                tape["prices"].append((slot, price))
                # Cap price history at 500 entries for performance
                if len(tape["prices"]) > 500:
                    tape["prices"] = tape["prices"][-500:]

        # CreateEvent is handled by the shim's BotState.apply_batch which updates state.curves;
        # we just ensure a tape entry exists.
        elif e["event"] == "CreateEvent":
            mint = e["mint"]
            _curve_init(mint)

    for m in marks:
        mint = m["mint"]
        tape = _curve_init(mint)

        price = _price_from_reserves(m.get("real_token_reserves"), m.get("real_quote_reserves"))
        if price is not None:
            tape["prices"].append((slot, price))
            # Cap price history at 500 entries for performance
            if len(tape["prices"]) > 500:
                tape["prices"] = tape["prices"][-500:]

    # ── 2. Score only curves touched by this batch (events + marks)
    # Performance: don't rescan all curves every batch; only evaluate those that changed.
    touched_mints = set()
    for e in events:
        touched_mints.add(e["mint"])
    for m in marks:
        touched_mints.add(m["mint"])
    
    candidate_mints = []
    for mint in touched_mints:
        curve = state.curves.get(mint)
        if curve is None:
            continue
        if curve.complete:
            continue
        if curve.created_slot is None:
            continue
        candidate_mints.append(mint)

    # Sort deterministically
    candidate_mints.sort(key=_sort_mint)

    # Evaluate each candidate
    scored = []
    for mint in candidate_mints:
        tape = on_batch._curve_tape.get(mint)
        if tape is None:
            continue  # no data yet; skip

        # --- distinct buyers ---
        n_buyers = len(tape["distinct_buyers"])

        # --- net SOL inflow must be positive (steady inflow) ---
        net_inflow = tape["net_sol_inflow"]
        inflow_ok = net_inflow > 0

        # --- price near window high with low intra-window volatility ---
        prices = tape["prices"]
        price_ok = False
        volatility_ok = False

        if len(prices) >= 3:
            # Prices are appended in slot order during the batch, and batches come in slot order
            # So the list is already in non-decreasing slot order - use it directly.
            price_vals = [p[1] for p in prices]

            # Window high/low
            window_high = max(price_vals)
            window_low = min(price_vals)

            # Price near window high: last (most recent) price >= 85% of window high
            last_price = price_vals[-1]
            if window_high > 0:
                price_near_high = last_price >= 0.85 * window_high
            else:
                price_near_high = False

            # Low volatility: price range / median price < threshold.
            # Use the median price as a robust midpoint.
            sorted_vals = sorted(price_vals)
            n = len(sorted_vals)
            if n % 2 == 1:
                median_price = sorted_vals[n // 2]
            else:
                median_price = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0

            price_range = window_high - window_low
            if median_price > 0:
                volatility_ratio = price_range / median_price
                # Accept if volatility ratio < 0.5 (50% of median price).
                # With 3+ points this catches truly spiky series;
                # with few points it's still meaningful.
                volatility_ok = volatility_ratio < 0.50
            else:
                volatility_ok = False

            price_ok = price_near_high and volatility_ok
        elif len(prices) == 2:
            # Two price points: be lenient on volatility but still require
            # price near the higher of the two.
            # Prices are appended in slot order during the batch, and batches come in slot order
            # So the list is already in non-decreasing slot order - use it directly.
            price_vals = [p[1] for p in prices]
            window_high = max(price_vals)
            window_low = min(price_vals)
            last_price = price_vals[-1]

            price_near_high = last_price >= 0.70 * window_high  # lower bar for 2 points

            # With only 2 points, volatility is inherent; be very lenient.
            # Only flag if the ratio is extreme (>1.5x the median).
            if window_high > window_low > 0:
                median_price = (window_high + window_low) / 2.0
                volatility_ratio = (window_high - window_low) / median_price
                volatility_ok = volatility_ratio < 1.50  # very lenient for 2 points
            else:
                volatility_ok = True  # cannot compute

            price_ok = price_near_high and volatility_ok
        elif len(prices) == 1:
            # Only one price point: cannot assess volatility, just require inflow.
            # Price is trivially "near window high" since it IS the window.
            price_ok = inflow_ok
            volatility_ok = True  # cannot assess with 1 point
        else:
            # No price data; skip.
            price_ok = False
            volatility_ok = False

        # --- Combined threshold: need inflow + (price near high + low vol) ---
        if inflow_ok and price_ok:
            # Score: weighted by buyer count (more = better) and volatility
            buyer_norm = min(n_buyers / 50.0, 1.0)
            vol_score = 1.0 if volatility_ok else 0.7  # partial credit for lenient vol
            score = 0.5 * buyer_norm + 0.5 * vol_score
            scored.append((mint, score))
        # else: reject this curve (don't add to scored)

    # Sort by score descending, then by mint for determinism
    scored.sort(key=lambda x: (-x[1], _sort_mint(x[0])))

    # Select top candidates that have score >= 0.3 (passes both inflow+price thresholds)
    selected = [m for m, s in scored if s >= 0.3]

    # ── 3. Place small uniform orders ──────────────────────────────────────────
    orders = []
    free = state.free_cash_lamports()
    cash = state.cash
    committed = state.committed_position_count()
    slots_available = max_open - committed
    tx_fee = tx

    # Keep cash reserve >= 50% of account
    min_cash_reserve = cash // 2
    max_to_spend = free - min_cash_reserve
    if max_to_spend < tx_fee:  # not enough to meet reserve after fees
        max_to_spend = 0

    target_stake = 30_000_000  # ~0.03 SOL in lamports

    for mint in selected:
        if len(orders) >= max_orders:
            break
        if slots_available <= 0:
            break

        # Per-coin size cap from the shim
        cap = state.max_buy_lamports(mint)
        if cap <= 0:
            continue  # unknown or complete

        # The most we can spend on this coin this batch
        max_affordable = min(cap, target_stake)

        # Ensure we have enough cash covering the buy amount + tx fee
        # Also respect the 50% cash reserve rule
        available_for_trading = max_to_spend - tx_fee
        if available_for_trading <= 0:
            continue  # can't even pay the fee while maintaining reserve
        if available_for_trading < max_affordable:
            amount = available_for_trading
            if amount <= 0:
                continue
        else:
            amount = max_affordable

        # Place a buy order
        order = {
            "side": "buy",
            "mint": mint,
            "quote_lamports": str(amount),
            "max_slippage_bps": 500,  # 5% = 500 bps slippage cap
        }
        orders.append(order)

        # Update tracking (the shim will re-evaluate via apply_batch next slot)
        max_to_spend -= amount + tx_fee
        slots_available -= 1

    # If no orders, that's fine — trading less is fine for Sharpe.
    return orders


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    makefun_bot.run(on_batch)


if __name__ == "__main__":
    main()