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

# Delay between consecutive arb order batches to avoid 429
INTER_ARB_DELAY_SEC = 2.0
# Delay between individual fallback orders
INTER_ORDER_DELAY_SEC = 0.5


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

    def __init__(self, client: KalshiClient | None, cfg: Config,
                 data_client: KalshiClient | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._data_client = data_client or client

    def get_available_balance_cents(self) -> int:
        """Query Kalshi for current available cash balance in cents."""
        if not self._data_client:
            return 0
        try:
            bal = self._data_client.get_balance()
            return int(bal.get("balance", 0))
        except Exception as e:
            logger.warning("Could not fetch balance: %s", e)
            return 0

    # ── Multi-outcome event arb ───────────────────────────────────────────────

    def execute_event_arb(self, opp: EventArbOpportunity) -> EventArbExecution:
        """Buy YES (or NO) on all legs of a mutually-exclusive event."""
        t0 = time.perf_counter()

        # Query live balance to cap quantity
        available = self.get_available_balance_cents() if not self._cfg.dry_run else 999999
        qty = self._calc_event_qty(opp, available)
        if qty < 1:
            return self._event_err(opp, f"quantity=0 (balance={available}¢)")

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
            legs = []
            for leg_data in opp.legs:
                t, p = leg_data[0], leg_data[1]  # (ticker, ask, bid) or (ticker, ask)
                legs.append(LegResult(ticker=t, side=side, price_cents=p, quantity=qty, status="dry_run"))
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
                "PARTIAL FILL %s: %d/%d legs placed, %d failed — ROLLING BACK",
                opp.event_ticker, len(placed), len(legs), len(failed),
            )
            for f in failed:
                logger.critical("  FAILED: %s %s — %s", f.side, f.ticker, f.error)

            # ROLLBACK: sell the placed legs to unwind naked exposure
            self._rollback_placed_legs(placed, opp.event_ticker)
        else:
            logger.error("EVENT ARB FAILED %s: all legs errored", opp.event_ticker)

        return result

    def _rollback_placed_legs(self, placed: List[LegResult], event_ticker: str) -> None:
        """Sell back placed legs to unwind partial/naked exposure."""
        if not self._client or not placed:
            return
        logger.warning("ROLLBACK %s: selling %d placed leg(s)", event_ticker, len(placed))
        for leg in placed:
            try:
                time.sleep(INTER_ORDER_DELAY_SEC)
                # Sell at 1¢ below our buy price for quick fill (market sell)
                sell_price = max(1, leg.price_cents - 1)
                self._client.create_order(
                    ticker=leg.ticker,
                    action="sell",
                    side=leg.side,
                    count=leg.quantity,
                    order_type="limit",
                    yes_price=sell_price if leg.side == "yes" else None,
                    no_price=sell_price if leg.side == "no" else None,
                )
                logger.info("  ROLLBACK sold %s %s qty=%d at %d¢",
                            leg.side, leg.ticker, leg.quantity, sell_price)
            except Exception as e:
                logger.error("  ROLLBACK FAILED %s: %s", leg.ticker, e)

    def _place_event_legs(
        self, legs: list[tuple], side: str, qty: int
    ) -> List[LegResult]:
        """Place all legs, using batch API for groups of ≤20."""
        assert self._client is not None
        results: List[LegResult] = []

        # Normalize: extract (ticker, price) from (ticker, ask, bid) tuples
        normalized = [(leg[0], leg[1]) for leg in legs]

        # Split into batches of 20 (Kalshi batch limit)
        for i in range(0, len(normalized), 20):
            batch_legs = normalized[i:i+20]
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
                # Fallback: try individual orders with rate limiting
                for ticker, price in batch_legs:
                    time.sleep(INTER_ORDER_DELAY_SEC)
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

    def _calc_event_qty(self, opp: EventArbOpportunity, available_balance: int = 999999) -> int:
        """Max sets constrained by config limits AND available balance."""
        set_cost = opp.sum_ask + opp.fee_total
        if set_cost <= 0:
            return 0
        max_by_cost = self._cfg.max_order_cents // set_cost
        max_by_balance = available_balance // set_cost
        qty = max(0, min(max_by_cost, max_by_balance, self._cfg.max_contracts, 50_000))
        if qty < max_by_cost:
            logger.info("Qty capped by balance: %d (would be %d without cap, balance=%d¢)",
                        qty, max_by_cost, available_balance)
        return qty

    def _event_err(self, opp: EventArbOpportunity, msg: str) -> EventArbExecution:
        legs = [LegResult(leg[0], "yes", leg[1], 0, "error", error=msg) for leg in opp.legs]
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
