"""
engine/profit_taker.py — Active profit-taking and stop-loss for open positions.

Runs every cycle alongside the scanner. For each open position:
  - Fetches current market bid price
  - TAKE PROFIT: if bid > cost_basis + min_take_profit → sell
  - STOP LOSS:   if bid < cost_basis × stop_loss_pct → sell
  - STALE EXIT:  if position age > max_hold_days → sell at market

This recycles capital much faster than waiting for event settlement.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from .client import KalshiClient, KalshiAPIError
from .config import Config

logger = logging.getLogger(__name__)

# ── Configuration knobs (cents) ───────────────────────────────────────────────

# Minimum profit in cents above cost basis (per contract) to trigger take-profit
DEFAULT_TAKE_PROFIT_CENTS = 4   # covers 2¢ sell fee + 2¢ minimum profit

# Stop loss: sell if value drops below this fraction of cost
DEFAULT_STOP_LOSS_PCT = 0.50    # sell if lost 50% of value

# If position is older than this many days, sell at any price to free capital
DEFAULT_MAX_HOLD_DAYS = 30

# Delay between sell operations to avoid rate limits
SELL_DELAY_SEC = 1.0


@dataclass
class PositionSnapshot:
    """Live snapshot of a single market position from Kalshi API."""
    ticker:           str
    event_ticker:     str
    side:             str       # inferred: "yes" if position > 0
    quantity:         int       # abs(position)
    market_exposure:  int       # cents at risk
    cost_basis_cents: int       # estimated per-contract cost
    current_bid:      int       # best bid we could sell at NOW
    current_ask:      int       # best ask (for reference)
    fees_paid:        int       # total fees already paid
    pnl_if_sell:      int       # estimated P&L if we sell at current bid

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis_cents <= 0:
            return 0.0
        return (self.current_bid - self.cost_basis_cents) / self.cost_basis_cents


@dataclass
class SellResult:
    """Result of a sell operation."""
    ticker:       str
    side:         str
    quantity:     int
    price:        int
    reason:       str    # "take_profit", "stop_loss", "stale_exit"
    status:       str    # "sold", "error", "dry_run"
    order_id:     str = ""
    error:        str = ""
    realized_pnl: int = 0


class ProfitTaker:
    """Monitors open positions and sells to take profit or cut losses."""

    def __init__(
        self,
        client: KalshiClient | None,
        data_client: KalshiClient,
        cfg: Config,
        take_profit_cents: int = DEFAULT_TAKE_PROFIT_CENTS,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    ) -> None:
        self._client = client
        self._data = data_client
        self._cfg = cfg
        self._take_profit_cents = take_profit_cents
        self._stop_loss_pct = stop_loss_pct
        self._max_hold_days = max_hold_days

    # ── Public interface ──────────────────────────────────────────────────────

    def check_and_sell(self) -> List[SellResult]:
        """
        Fetch all open positions, evaluate TP/SL, sell if triggered.
        Returns list of sell results (may be empty).
        """
        snapshots = self._fetch_position_snapshots()
        if not snapshots:
            return []

        results: List[SellResult] = []
        for snap in snapshots:
            action = self._evaluate(snap)
            if action is None:
                continue

            reason, sell_price = action
            logger.info(
                "%s %s  qty=%d  cost=%d¢  bid=%d¢  pnl=%+d¢ (%.0f%%)  → %s",
                snap.ticker, snap.side, snap.quantity,
                snap.cost_basis_cents, snap.current_bid,
                snap.pnl_if_sell, snap.pnl_pct * 100, reason,
            )

            result = self._sell(snap, sell_price, reason)
            results.append(result)

            if result.status == "sold":
                logger.info(
                    "  SOLD %s  %d contracts at %d¢  realized=%+d¢  (%s)",
                    snap.ticker, snap.quantity, sell_price,
                    result.realized_pnl, reason,
                )
            elif result.status == "error":
                logger.error("  SELL FAILED %s: %s", snap.ticker, result.error)

            # Rate limit between sells
            time.sleep(SELL_DELAY_SEC)

        return results

    # ── Evaluation logic ──────────────────────────────────────────────────────

    def _evaluate(self, snap: PositionSnapshot) -> Optional[tuple[str, int]]:
        """
        Decide whether to sell this position.
        Returns (reason, sell_price) or None to hold.
        """
        cost = snap.cost_basis_cents
        bid = snap.current_bid

        if bid <= 0:
            # No bid available — can't sell
            return None

        # Fee for selling = 2¢ (taker fee assumed)
        sell_fee = 2
        net_after_sell = bid - sell_fee

        # Take profit: net proceeds > cost basis + minimum take
        if net_after_sell >= cost + self._take_profit_cents:
            return ("take_profit", bid)

        # Stop loss: bid has fallen below threshold
        if cost > 0 and bid < cost * self._stop_loss_pct:
            return ("stop_loss", bid)

        # Hold
        return None

    # ── Fetching live position data ───────────────────────────────────────────

    def _fetch_position_snapshots(self) -> List[PositionSnapshot]:
        """Query Kalshi API for current positions + market prices."""
        try:
            pos_data = self._data.get_positions()
        except Exception as e:
            logger.error("Failed to fetch positions: %s", e)
            return []

        market_positions = pos_data.get("market_positions", [])
        if not market_positions:
            return []

        snapshots: List[PositionSnapshot] = []
        for mp in market_positions:
            ticker = mp.get("ticker", "")
            position = mp.get("position", 0)
            if position == 0:
                continue

            quantity = abs(position)
            side = "yes" if position > 0 else "no"
            market_exposure = mp.get("market_exposure", 0)
            fees_paid = mp.get("fees_paid", 0)
            total_traded = mp.get("total_traded", 0)

            # Cost basis per contract = total_traded / quantity
            cost_basis = total_traded // quantity if quantity > 0 else 0

            # Fetch current market prices
            bid, ask = self._fetch_market_price(ticker, side)

            # Sell fee estimate = 2¢ per contract
            sell_fee_per = 2
            pnl_if_sell = (bid - cost_basis - sell_fee_per) * quantity if bid > 0 else -market_exposure

            # Derive event ticker (everything before the last dash-segment)
            parts = ticker.rsplit("-", 1)
            event_ticker = parts[0] if len(parts) > 1 else ticker

            snapshots.append(PositionSnapshot(
                ticker=ticker,
                event_ticker=event_ticker,
                side=side,
                quantity=quantity,
                market_exposure=market_exposure,
                cost_basis_cents=cost_basis,
                current_bid=bid,
                current_ask=ask,
                fees_paid=fees_paid,
                pnl_if_sell=pnl_if_sell,
            ))

        return snapshots

    def _fetch_market_price(self, ticker: str, side: str) -> tuple[int, int]:
        """
        Get current bid/ask for a market.
        Returns (bid, ask) in cents. bid = what we can sell at.
        """
        try:
            data = self._data.get_market(ticker)
            mkt = data.get("market", data)

            if side == "yes":
                bid = mkt.get("yes_bid", 0) or 0
                ask = mkt.get("yes_ask", 0) or 0
            else:
                bid = mkt.get("no_bid", 0) or 0
                ask = mkt.get("no_ask", 0) or 0

            return (bid, ask)
        except Exception as e:
            logger.warning("Could not fetch price for %s: %s", ticker, e)
            return (0, 0)

    # ── Selling ───────────────────────────────────────────────────────────────

    def _sell(self, snap: PositionSnapshot, price: int, reason: str) -> SellResult:
        """Place a sell order for the position."""
        if self._cfg.dry_run:
            sell_fee = 2 * snap.quantity
            realized = (price - snap.cost_basis_cents) * snap.quantity - sell_fee
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=snap.quantity, price=price,
                reason=reason, status="dry_run",
                realized_pnl=realized,
            )

        if not self._client:
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=snap.quantity, price=price,
                reason=reason, status="error",
                error="No trading client (dry_run mode)",
            )

        try:
            import uuid
            resp = self._client.create_order(
                ticker=snap.ticker,
                action="sell",
                side=snap.side,
                count=snap.quantity,
                order_type="limit",
                yes_price=price if snap.side == "yes" else None,
                no_price=price if snap.side == "no" else None,
                client_order_id=str(uuid.uuid4()),
            )
            order = resp.get("order", resp)
            order_id = order.get("order_id", "")
            fill_count = order.get("fill_count", 0)

            sell_fee = 2 * snap.quantity
            realized = (price - snap.cost_basis_cents) * snap.quantity - sell_fee

            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=snap.quantity, price=price,
                reason=reason, status="sold",
                order_id=order_id,
                realized_pnl=realized,
            )
        except Exception as e:
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=snap.quantity, price=price,
                reason=reason, status="error",
                error=str(e),
            )

    # ── Reporting ────────────────────────────────────────────────────────────

    def get_position_report(self) -> List[PositionSnapshot]:
        """Return current position snapshots for display."""
        return self._fetch_position_snapshots()
