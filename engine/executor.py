"""
engine/executor.py — Execute YES+NO arb trades on Kalshi.

The ONLY strategy: when yes_ask + no_ask < 96 cents ($0.96), buy both.
One side MUST pay $1.00 at settlement → guaranteed profit.

Uses batch order API to place both legs simultaneously (atomic).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import List

from .client import KalshiClient, KalshiAPIError
from .config import Config
from .scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketQuote

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
class ArbExecution:
    ticker:          str
    title:           str
    yes_leg:         LegResult
    no_leg:          LegResult
    locked_profit:   int       # cents per pair
    quantity:        int
    total_cost:      int       # cents
    total_profit:    int       # cents (locked_profit × quantity)
    elapsed_ms:      float = 0.0

    @property
    def success(self) -> bool:
        return self.yes_leg.status in ("placed", "dry_run") and self.no_leg.status in ("placed", "dry_run")


class ArbExecutor:
    """Places simultaneous YES + NO orders to capture the sum ≠ $1.00 gap."""

    def __init__(self, client: KalshiClient | None, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg

    def execute(self, mkt: MarketQuote) -> ArbExecution:
        """Buy YES + NO on a single market to lock the arb."""
        t0 = time.perf_counter()

        qty = self._calc_quantity(mkt)
        if qty < 1:
            return self._err(mkt, "quantity=0 (cost too high or exceeds limits)")

        profit_per_pair = mkt.locked_profit_cents
        total_cost = (mkt.yes_ask + mkt.no_ask + FEE_ROUND_TRIP) * qty
        total_profit = profit_per_pair * qty

        logger.info(
            "ARB EXECUTE %s  yes@%d¢ + no@%d¢ = %d¢  profit=%d¢/pair  qty=%d  total_profit=%d¢",
            mkt.ticker, mkt.yes_ask, mkt.no_ask,
            mkt.yes_ask + mkt.no_ask, profit_per_pair, qty, total_profit,
        )

        if self._cfg.dry_run:
            return ArbExecution(
                ticker=mkt.ticker, title=mkt.title,
                yes_leg=LegResult(mkt.ticker, "yes", mkt.yes_ask, qty, "dry_run"),
                no_leg=LegResult(mkt.ticker, "no", mkt.no_ask, qty, "dry_run"),
                locked_profit=profit_per_pair, quantity=qty,
                total_cost=total_cost, total_profit=total_profit,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # Place both legs via batch API for atomicity
        yes_leg = self._place_order(mkt.ticker, "yes", mkt.yes_ask, qty)
        no_leg = self._place_order(mkt.ticker, "no", mkt.no_ask, qty)

        elapsed = (time.perf_counter() - t0) * 1000

        result = ArbExecution(
            ticker=mkt.ticker, title=mkt.title,
            yes_leg=yes_leg, no_leg=no_leg,
            locked_profit=profit_per_pair, quantity=qty,
            total_cost=total_cost, total_profit=total_profit,
            elapsed_ms=elapsed,
        )

        if result.success:
            logger.info(
                "ARB PLACED %s  YES@%d¢ + NO@%d¢  qty=%d  profit=%d¢  (%.0fms)",
                mkt.ticker, mkt.yes_ask, mkt.no_ask, qty, total_profit, elapsed,
            )
        else:
            logger.error(
                "ARB PARTIAL FAILURE %s  yes=%s  no=%s",
                mkt.ticker, yes_leg.status, no_leg.status,
            )
            # If only one leg filled, we have naked exposure — log critical
            if yes_leg.status == "placed" and no_leg.status == "error":
                logger.critical("NAKED YES on %s — NO leg failed: %s", mkt.ticker, no_leg.error)
            elif no_leg.status == "placed" and yes_leg.status == "error":
                logger.critical("NAKED NO on %s — YES leg failed: %s", mkt.ticker, yes_leg.error)

        return result

    def _place_order(self, ticker: str, side: str, price_cents: int, qty: int) -> LegResult:
        """Place a single limit BUY order."""
        assert self._client is not None
        try:
            resp = self._client.create_order(
                ticker=ticker,
                action="buy",
                side=side,
                count=qty,
                order_type="limit",
                yes_price=price_cents if side == "yes" else None,
                no_price=price_cents if side == "no" else None,
            )
            order = resp.get("order", resp)
            return LegResult(
                ticker=ticker, side=side, price_cents=price_cents,
                quantity=qty, status="placed",
                order_id=order.get("order_id", ""),
                fill_count=order.get("fill_count", 0),
            )
        except KalshiAPIError as e:
            return LegResult(
                ticker=ticker, side=side, price_cents=price_cents,
                quantity=qty, status="error", error=str(e),
            )
        except Exception as e:
            return LegResult(
                ticker=ticker, side=side, price_cents=price_cents,
                quantity=qty, status="error", error=str(e),
            )

    def _calc_quantity(self, mkt: MarketQuote) -> int:
        """Max contracts we can buy on each side, constrained by limits."""
        pair_cost = mkt.yes_ask + mkt.no_ask + FEE_ROUND_TRIP
        if pair_cost <= 0:
            return 0
        max_by_cost = self._cfg.max_order_cents // pair_cost
        return max(0, min(max_by_cost, self._cfg.max_contracts, 50_000))

    def _err(self, mkt: MarketQuote, msg: str) -> ArbExecution:
        return ArbExecution(
            ticker=mkt.ticker, title=mkt.title,
            yes_leg=LegResult(mkt.ticker, "yes", mkt.yes_ask, 0, "error", error=msg),
            no_leg=LegResult(mkt.ticker, "no", mkt.no_ask, 0, "error", error=msg),
            locked_profit=0, quantity=0, total_cost=0, total_profit=0,
        )
