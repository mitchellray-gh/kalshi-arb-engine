"""
engine/executor.py — Execute multi-outcome event arb trades on Kalshi.

PRIMARY STRATEGY: Buy YES (or NO) on ALL markets in a mutually-exclusive event.
  - BUY-ALL-YES: one market MUST settle YES → pay $1.00 → profit = 100 - sum(asks) - fees
  - BUY-ALL-NO: N-1 markets settle NO → pay (N-1)×$1.00 → profit = revenue - sum(no_asks) - fees

Uses batch order API (up to 20 orders per batch) for near-atomic execution.
Falls back to sequential orders if legs > 20.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import List

from .client import KalshiClient, KalshiAPIError
from .config import Config
from .scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, EventArbOpportunity, MarketQuote

logger = logging.getLogger(__name__)


@dataclass
class LegResult:
    ticker:       str
    side:         str      # "yes" or "no"
    price_cents:  int
    quantity:     int
    status:       str      # "placed", "dry_run", "error"
    order_id:     str = ""
    error:        str = ""
    fill_count:   int = 0


@dataclass
class EventArbExecution:
    """Result of executing a multi-outcome event arb."""
    event_ticker:    str
    event_title:     str
    arb_type:        str
    legs:            List[LegResult]
    profit_per_set:  int       # cents
    quantity:        int
    total_cost:      int       # cents
    total_profit:    int       # cents
    elapsed_ms:      float = 0.0

    @property
    def success(self) -> bool:
        return all(l.status in ("placed", "dry_run") for l in self.legs)

    @property
    def partial_fill(self) -> bool:
        placed = sum(1 for l in self.legs if l.status == "placed")
        errors = sum(1 for l in self.legs if l.status == "error")
        return placed > 0 and errors > 0

    @property
    def n_legs(self) -> int:
        return len(self.legs)


@dataclass
class SingleArbExecution:
    """Result of executing a single-market YES+NO arb."""
    ticker:          str
    title:           str
    yes_leg:         LegResult
    no_leg:          LegResult
    locked_profit:   int
    quantity:        int
    total_cost:      int
    total_profit:    int
    elapsed_ms:      float = 0.0

    @property
    def success(self) -> bool:
        return self.yes_leg.status in ("placed", "dry_run") and self.no_leg.status in ("placed", "dry_run")


class ArbExecutor:
    """Places orders to capture multi-outcome and single-market arb."""

    def __init__(self, client: KalshiClient | None, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg

    # ── Multi-outcome event arb ───────────────────────────────────────────────

    def execute_event_arb(self, opp: EventArbOpportunity) -> EventArbExecution:
        """Buy YES (or NO) on all legs of a mutually-exclusive event."""
        t0 = time.perf_counter()

        qty = self._calc_event_qty(opp)
        if qty < 1:
            return self._event_err(opp, "quantity=0")

        set_cost = opp.sum_ask + opp.fee_total
        total_cost = set_cost * qty
        total_profit = opp.profit_per_set * qty

        side = "yes" if opp.arb_type == "buy_all_yes" else "no"

        logger.info(
            "EVENT ARB EXECUTE %s  %s  %d legs  sum=%d  profit=%d¢/set  qty=%d  total=%d¢",
            opp.event_ticker, opp.arb_type, opp.n_markets,
            opp.sum_ask, opp.profit_per_set, qty, total_profit,
        )

        if self._cfg.dry_run:
            legs = [
                LegResult(ticker=t, side=side, price_cents=p, quantity=qty, status="dry_run")
                for t, p in opp.legs
            ]
            return EventArbExecution(
                event_ticker=opp.event_ticker, event_title=opp.event_title,
                arb_type=opp.arb_type, legs=legs,
                profit_per_set=opp.profit_per_set, quantity=qty,
                total_cost=total_cost, total_profit=total_profit,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # Execute via batch API (max 20 per batch)
        legs = self._place_event_legs(opp.legs, side, qty)

        elapsed = (time.perf_counter() - t0) * 1000
        result = EventArbExecution(
            event_ticker=opp.event_ticker, event_title=opp.event_title,
            arb_type=opp.arb_type, legs=legs,
            profit_per_set=opp.profit_per_set, quantity=qty,
            total_cost=total_cost, total_profit=total_profit,
            elapsed_ms=elapsed,
        )

        if result.success:
            logger.info(
                "EVENT ARB PLACED %s  %d legs  qty=%d  profit=%d¢  (%.0fms)",
                opp.event_ticker, len(legs), qty, total_profit, elapsed,
            )
        elif result.partial_fill:
            placed = [l for l in legs if l.status == "placed"]
            failed = [l for l in legs if l.status == "error"]
            logger.critical(
                "PARTIAL FILL %s: %d/%d legs placed, %d failed — NAKED EXPOSURE",
                opp.event_ticker, len(placed), len(legs), len(failed),
            )
            for f in failed:
                logger.critical("  FAILED: %s %s — %s", f.side, f.ticker, f.error)
        else:
            logger.error("EVENT ARB FAILED %s: all legs errored", opp.event_ticker)

        return result

    def _place_event_legs(
        self, legs: list[tuple], side: str, qty: int
    ) -> List[LegResult]:
        """Place all legs, using batch API for groups of ≤20."""
        assert self._client is not None
        results: List[LegResult] = []

        # Split into batches of 20 (Kalshi batch limit)
        for i in range(0, len(legs), 20):
            batch_legs = legs[i:i+20]
            orders = []
            for ticker, price in batch_legs:
                orders.append({
                    "ticker": ticker,
                    "action": "buy",
                    "side": side,
                    "count": qty,
                    "type": "limit",
                    f"{side}_price": price,
                    "client_order_id": str(uuid.uuid4()),
                })

            try:
                resp = self._client.batch_create_orders(orders)
                batch_orders = resp.get("orders", [])
                for j, (ticker, price) in enumerate(batch_legs):
                    if j < len(batch_orders):
                        o = batch_orders[j]
                        results.append(LegResult(
                            ticker=ticker, side=side, price_cents=price,
                            quantity=qty, status="placed",
                            order_id=o.get("order_id", ""),
                            fill_count=o.get("fill_count", 0),
                        ))
                    else:
                        results.append(LegResult(
                            ticker=ticker, side=side, price_cents=price,
                            quantity=qty, status="error",
                            error="Missing from batch response",
                        ))
            except KalshiAPIError as e:
                logger.error("Batch order failed: %s", e)
                # Fallback: try individual orders
                for ticker, price in batch_legs:
                    results.append(self._place_single_order(ticker, side, price, qty))
            except Exception as e:
                for ticker, price in batch_legs:
                    results.append(LegResult(
                        ticker=ticker, side=side, price_cents=price,
                        quantity=qty, status="error", error=str(e),
                    ))

        return results

    def _place_single_order(self, ticker: str, side: str, price: int, qty: int) -> LegResult:
        """Place a single limit BUY order."""
        assert self._client is not None
        try:
            resp = self._client.create_order(
                ticker=ticker, action="buy", side=side, count=qty,
                order_type="limit",
                yes_price=price if side == "yes" else None,
                no_price=price if side == "no" else None,
            )
            order = resp.get("order", resp)
            return LegResult(
                ticker=ticker, side=side, price_cents=price,
                quantity=qty, status="placed",
                order_id=order.get("order_id", ""),
                fill_count=order.get("fill_count", 0),
            )
        except Exception as e:
            return LegResult(
                ticker=ticker, side=side, price_cents=price,
                quantity=qty, status="error", error=str(e),
            )

    def _calc_event_qty(self, opp: EventArbOpportunity) -> int:
        """Max sets constrained by config limits."""
        set_cost = opp.sum_ask + opp.fee_total
        if set_cost <= 0:
            return 0
        max_by_cost = self._cfg.max_order_cents // set_cost
        return max(0, min(max_by_cost, self._cfg.max_contracts, 50_000))

    def _event_err(self, opp: EventArbOpportunity, msg: str) -> EventArbExecution:
        legs = [LegResult(t, "yes", p, 0, "error", error=msg) for t, p in opp.legs]
        return EventArbExecution(
            event_ticker=opp.event_ticker, event_title=opp.event_title,
            arb_type=opp.arb_type, legs=legs,
            profit_per_set=0, quantity=0, total_cost=0, total_profit=0,
        )

    # ── Single-market arb (legacy, rare) ──────────────────────────────────────

    def execute_single(self, mkt: MarketQuote) -> SingleArbExecution:
        """Buy YES + NO on a single market."""
        t0 = time.perf_counter()
        pair_cost = mkt.yes_ask + mkt.no_ask + FEE_ROUND_TRIP
        qty = max(0, min(self._cfg.max_order_cents // pair_cost, self._cfg.max_contracts))
        if qty < 1:
            yes_leg = LegResult(mkt.ticker, "yes", mkt.yes_ask, 0, "error", error="qty=0")
            no_leg = LegResult(mkt.ticker, "no", mkt.no_ask, 0, "error", error="qty=0")
            return SingleArbExecution(mkt.ticker, mkt.title, yes_leg, no_leg, 0, 0, 0, 0)

        profit = mkt.locked_profit_cents
        total_cost = pair_cost * qty
        total_profit = profit * qty

        if self._cfg.dry_run:
            return SingleArbExecution(
                mkt.ticker, mkt.title,
                LegResult(mkt.ticker, "yes", mkt.yes_ask, qty, "dry_run"),
                LegResult(mkt.ticker, "no", mkt.no_ask, qty, "dry_run"),
                profit, qty, total_cost, total_profit,
                (time.perf_counter() - t0) * 1000,
            )

        yes_leg = self._place_single_order(mkt.ticker, "yes", mkt.yes_ask, qty)
        no_leg = self._place_single_order(mkt.ticker, "no", mkt.no_ask, qty)
        return SingleArbExecution(
            mkt.ticker, mkt.title, yes_leg, no_leg,
            profit, qty, total_cost, total_profit,
            (time.perf_counter() - t0) * 1000,
        )
