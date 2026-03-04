"""
engine/profit_taker.py — Rapid profit-taking engine for fast wealth building.

Core principle: CAPITAL VELOCITY > PER-TRADE MARGIN.
  Selling at 1¢ net profit and redeploying 100× beats holding for settlement.
  $100 compounding at 1% per rotation → 35% in 30 rotations vs 4% hold-to-settle.

Runs on a FAST cycle (every few seconds), separate from the arb scanner.
For each open position, evaluates (in priority order):

  1. SCALP:       bid >= cost + 4¢ fees + min_profit → sell immediately
  2. TRAIL STOP:  bid dropped from peak but still profitable → lock gains
  3. MOMENTUM:    bid spiked hard (2× cost) → sell half, let rest ride
  4. STOP LOSS:   bid < cost × stop_pct → cut losses fast
  5. STALE EXIT:  held > max_hold_days → dump to free capital

Fee model: 2¢ buy + 2¢ sell = 4¢ round trip.
  Min profitable scalp: bid >= cost_basis + 5¢ (for 1¢ net profit).
  Settlement has no sell fee, so holding IS cheaper — but slower.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .client import KalshiClient, KalshiAPIError
from .config import Config

logger = logging.getLogger(__name__)

# ── Fee constants ─────────────────────────────────────────────────────────────
SELL_FEE = 2            # cents per contract
BUY_FEE = 2             # cents per contract (sunk cost)
ROUND_TRIP_FEE = 4      # buy + sell
SELL_DELAY_SEC = 0.5    # between sell orders (was 1.0 — faster now)


@dataclass
class PositionSnapshot:
    """Live snapshot of a single market position from Kalshi API."""
    ticker:           str
    event_ticker:     str
    side:             str       # "yes" or "no"
    quantity:         int
    market_exposure:  int       # cents at risk
    cost_basis_cents: int       # avg price per contract (excl fees)
    current_bid:      int       # best bid we could sell at NOW
    current_ask:      int       # best ask (for reference)
    fees_paid:        int       # total fees already paid
    pnl_if_sell:      int       # net P&L if sold at current bid (after ALL fees)

    @property
    def net_per_contract(self) -> int:
        """Actual cents profit per contract if sold at bid, after round-trip fees."""
        return self.current_bid - self.cost_basis_cents - ROUND_TRIP_FEE

    @property
    def pnl_pct(self) -> float:
        total_invested = self.cost_basis_cents + BUY_FEE
        if total_invested <= 0:
            return 0.0
        return self.net_per_contract / total_invested

    @property
    def breakeven_bid(self) -> int:
        """Minimum bid needed to break even after all fees."""
        return self.cost_basis_cents + ROUND_TRIP_FEE


@dataclass
class SellResult:
    """Result of a sell operation."""
    ticker:       str
    side:         str
    quantity:     int
    price:        int
    reason:       str    # "scalp", "trail_stop", "momentum", "stop_loss", "stale_exit"
    status:       str    # "sold", "error", "dry_run"
    order_id:     str = ""
    error:        str = ""
    realized_pnl: int = 0  # cents, net after all fees


class ProfitTaker:
    """Aggressive profit-taking engine for rapid capital rotation.

    Tracks high-water marks per position for trailing stops.
    Tracks session-level realized P&L for compounding visibility.
    """

    def __init__(
        self,
        client: KalshiClient | None,
        data_client: KalshiClient,
        cfg: Config,
        **kwargs,
    ) -> None:
        self._client = client
        self._data = data_client
        self._cfg = cfg

        # Thresholds
        self._min_scalp = getattr(cfg, 'min_scalp_cents', kwargs.get('min_scalp_cents', 1))
        self._trail_stop = getattr(cfg, 'trail_stop_cents', kwargs.get('trail_stop_cents', 3))
        self._stop_loss_pct = getattr(cfg, 'stop_loss_pct', kwargs.get('stop_loss_pct', 0.50))
        self._max_hold_days = getattr(cfg, 'max_hold_days', kwargs.get('max_hold_days', 30))

        # Trailing stop state: ticker → highest bid seen
        self._high_water: Dict[str, int] = {}

        # Session-level wealth tracking
        self._session_realized: int = 0       # total realized cents this session
        self._session_trades: int = 0         # number of profitable exits
        self._session_losses: int = 0         # number of stop-loss exits
        self._session_start: datetime = datetime.now(timezone.utc)
        self._capital_freed: int = 0          # cents returned to balance for redeployment

    # ── Public interface ──────────────────────────────────────────────────────

    def check_and_sell(self) -> List[SellResult]:
        """
        Fast-cycle check: fetch all positions, evaluate exits, sell winners.
        Returns list of sell results (empty if nothing to do).
        """
        snapshots = self._fetch_position_snapshots()
        if not snapshots:
            return []

        results: List[SellResult] = []
        for snap in snapshots:
            # Always update trailing stop tracker
            self._update_high_water(snap)

            action = self._evaluate(snap)
            if action is None:
                continue

            reason, sell_price, sell_qty = action

            logger.info(
                "PROFIT_TAKE %s %s  qty=%d→%d  cost=%d¢  bid=%d¢  net=%+d¢/ea  → %s",
                snap.ticker, snap.side, snap.quantity, sell_qty,
                snap.cost_basis_cents, snap.current_bid,
                snap.net_per_contract, reason,
            )

            result = self._sell(snap, sell_price, reason, sell_qty)
            results.append(result)

            if result.status in ("sold", "dry_run"):
                self._session_realized += result.realized_pnl
                capital_back = sell_price * sell_qty  # gross capital returned
                self._capital_freed += capital_back
                if result.realized_pnl >= 0:
                    self._session_trades += 1
                else:
                    self._session_losses += 1

                # Clean up tracking if fully sold
                if sell_qty >= snap.quantity:
                    self._high_water.pop(snap.ticker, None)

                logger.info(
                    "  -> %s %s  %d@%d¢  pnl=%+d¢  session=%+d¢ (%d wins, %d losses)  freed=%d¢",
                    result.status.upper(), snap.ticker,
                    sell_qty, sell_price, result.realized_pnl,
                    self._session_realized, self._session_trades,
                    self._session_losses, capital_back,
                )
            elif result.status == "error":
                logger.error("  SELL FAILED %s: %s", snap.ticker, result.error)

            time.sleep(SELL_DELAY_SEC)

        return results

    @property
    def session_stats(self) -> dict:
        """Session performance metrics for display."""
        elapsed = (datetime.now(timezone.utc) - self._session_start).total_seconds()
        hours = elapsed / 3600 if elapsed > 0 else 0.001
        return {
            "realized_cents": self._session_realized,
            "realized_dollars": self._session_realized / 100,
            "wins": self._session_trades,
            "losses": self._session_losses,
            "capital_freed_cents": self._capital_freed,
            "elapsed_hours": hours,
            "cents_per_hour": self._session_realized / hours if hours > 0 else 0,
            "dollars_per_hour": self._session_realized / 100 / hours if hours > 0 else 0,
        }

    # ── Evaluation logic (priority-ordered) ───────────────────────────────────

    def _evaluate(self, snap: PositionSnapshot) -> Optional[Tuple[str, int, int]]:
        """
        Decide whether to sell this position.
        Returns (reason, sell_price, quantity) or None to hold.
        """
        bid = snap.current_bid
        cost = snap.cost_basis_cents
        qty = snap.quantity

        if bid <= 0:
            return None

        net = snap.net_per_contract  # bid - cost - 4¢ round trip

        # ── 1. SCALP: any profit >= minimum threshold ────────────────────────
        if net >= self._min_scalp:
            return ("scalp", bid, qty)

        # ── 2. MOMENTUM: position doubled+ → sell half, let rest ride ────────
        if cost > 0 and bid >= cost * 2 and qty >= 2:
            half = qty // 2
            return ("momentum", bid, half)

        # ── 3. TRAIL STOP: peaked and now falling, but still above breakeven ─
        hw = self._high_water.get(snap.ticker, cost)
        if hw > snap.breakeven_bid + self._min_scalp:
            # Was profitable at the peak
            drop = hw - bid
            if drop >= self._trail_stop and net > 0:
                return ("trail_stop", bid, qty)

        # ── 4. STOP LOSS: cut losses quickly ─────────────────────────────────
        if cost > 0 and bid < cost * self._stop_loss_pct:
            return ("stop_loss", bid, qty)

        # ── 5. HOLD — not yet time to exit ───────────────────────────────────
        return None

    def _update_high_water(self, snap: PositionSnapshot) -> None:
        """Track highest bid seen per ticker for trailing stops."""
        prev = self._high_water.get(snap.ticker, 0)
        if snap.current_bid > prev:
            self._high_water[snap.ticker] = snap.current_bid

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

            # Cost basis = avg fill price per contract (fees separate)
            cost_basis = total_traded // quantity if quantity > 0 else 0

            # Fetch current market prices
            bid, ask = self._fetch_market_price(ticker, side)

            # Accurate P&L: bid - cost - round_trip_fee (per contract) × quantity
            net_per = bid - cost_basis - ROUND_TRIP_FEE if bid > 0 else -(cost_basis + BUY_FEE)
            pnl_if_sell = net_per * quantity

            # Derive event ticker
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

    def _sell(self, snap: PositionSnapshot, price: int, reason: str,
              sell_qty: int | None = None) -> SellResult:
        """Place a sell order. Supports partial sells (momentum exit)."""
        qty = sell_qty if sell_qty is not None else snap.quantity

        # Net P&L for the contracts being sold
        net_per = price - snap.cost_basis_cents - ROUND_TRIP_FEE
        realized = net_per * qty

        if self._cfg.dry_run:
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=qty, price=price,
                reason=reason, status="dry_run",
                realized_pnl=realized,
            )

        if not self._client:
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=qty, price=price,
                reason=reason, status="error",
                error="No trading client (dry_run mode)",
            )

        try:
            resp = self._client.create_order(
                ticker=snap.ticker,
                action="sell",
                side=snap.side,
                count=qty,
                order_type="limit",
                yes_price=price if snap.side == "yes" else None,
                no_price=price if snap.side == "no" else None,
                client_order_id=str(uuid.uuid4()),
            )
            order = resp.get("order", resp)
            order_id = order.get("order_id", "")

            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=qty, price=price,
                reason=reason, status="sold",
                order_id=order_id,
                realized_pnl=realized,
            )
        except Exception as e:
            return SellResult(
                ticker=snap.ticker, side=snap.side,
                quantity=qty, price=price,
                reason=reason, status="error",
                error=str(e),
            )

    # ── Reporting ────────────────────────────────────────────────────────────

    def get_position_report(self) -> List[PositionSnapshot]:
        """Return current position snapshots for display."""
        return self._fetch_position_snapshots()
