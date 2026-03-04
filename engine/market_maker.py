"""
engine/market_maker.py — Spread-capture market-making engine.

Places limit orders on both sides of the spread to capture the bid-ask gap.
Maker fee = 0¢ on Kalshi, so profit = sell_price - buy_price per contract.

Workflow per target market:
  1. Place limit BUY at (bid + 1) → become best bid (maker, 0¢ fee)
  2. Wait for buy fill (or cancel after timeout)
  3. When filled: place limit SELL at (ask) or (ask - 1) → capture spread
  4. Wait for sell fill (or stop-loss at taker price)
  5. Track realized P&L per round-trip

Risk management:
  - MAX_INVENTORY per market (% of balance)
  - BUY_TIMEOUT: cancel unfilled buy after N seconds
  - STOP_LOSS: emergency taker-sell if price drops > threshold
  - MAX_TOTAL_EXPOSURE: cap total $ at risk
  - No holding through settlement unless intentional
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .client import KalshiClient, KalshiAPIError
from .config import Config
from .spread_scanner import SpreadTarget, MAKER_FEE, TAKER_FEE

logger = logging.getLogger(__name__)


class OrderState(Enum):
    PENDING = "pending"         # order placed, awaiting fill
    FILLED = "filled"           # fully filled
    PARTIAL = "partial"         # partially filled
    CANCELLED = "cancelled"     # cancelled by us
    ERROR = "error"


@dataclass
class MMOrder:
    """Tracks a single market-making order."""
    ticker:         str
    side:           str         # "yes" or "no"
    action:         str         # "buy" or "sell"
    price:          int         # limit price in cents
    quantity:       int
    order_id:       str = ""
    state:          OrderState = OrderState.PENDING
    filled_qty:     int = 0
    placed_at:      float = 0.0  # time.time()
    filled_at:      float = 0.0
    error:          str = ""


@dataclass
class RoundTrip:
    """A completed buy+sell pair."""
    ticker:       str
    buy_price:    int
    sell_price:   int
    quantity:     int
    profit_cents: int       # (sell - buy) * qty
    buy_fee:      int       # 0 if maker
    sell_fee:     int       # 0 if maker, TAKER_FEE if emergency
    net_profit:   int       # profit - fees
    completed_at: str = ""


@dataclass
class MMPosition:
    """Current inventory in a single market."""
    ticker:         str
    side:           str = "yes"
    quantity:       int = 0
    avg_cost:       int = 0     # avg buy price
    buy_order:      Optional[MMOrder] = None
    sell_order:     Optional[MMOrder] = None
    high_water:     int = 0     # highest bid seen since entry


class MarketMaker:
    """Spread-capture market-making engine.

    Manages a portfolio of maker limit orders across multiple markets.
    Core loop: scan → place buys → monitor fills → place sells → repeat.
    """

    def __init__(
        self,
        client: KalshiClient | None,
        data_client: KalshiClient,
        cfg: Config,
    ) -> None:
        self._client = client
        self._data = data_client
        self._cfg = cfg

        # Settings from config
        self._min_spread = getattr(cfg, 'mm_min_spread', 2)
        self._buy_timeout = getattr(cfg, 'mm_buy_timeout', 120)   # seconds
        self._stop_loss_cents = getattr(cfg, 'mm_stop_loss', 5)   # max loss per contract
        self._max_per_market = getattr(cfg, 'mm_max_per_market', 30)  # % of balance
        self._max_total_pct = getattr(cfg, 'mm_max_total_exposure', 80)  # % of balance

        # Active positions: ticker → MMPosition
        self._positions: Dict[str, MMPosition] = {}

        # Session tracking
        self._round_trips: List[RoundTrip] = []
        self._session_start = time.time()
        self._total_bought = 0
        self._total_sold = 0

    # ── Public interface ──────────────────────────────────────────────────────

    def place_spread_orders(self, targets: List[SpreadTarget]) -> int:
        """Place buy orders on the best spread targets.

        Returns number of new buy orders placed.
        """
        if not self._client and not self._cfg.dry_run:
            return 0

        available = self._get_available_balance()
        max_total = int(available * self._max_total_pct / 100)
        current_exposure = sum(p.quantity * p.avg_cost for p in self._positions.values())
        remaining = max(0, max_total - current_exposure)

        placed = 0
        for target in targets:
            if target.profit_per_rt < 1:
                continue
            if target.ticker in self._positions:
                continue  # already have position/pending order
            if remaining <= 0:
                break

            # Calculate quantity: cap by per-market limit and remaining budget
            max_market = int(available * self._max_per_market / 100)
            max_by_budget = min(remaining, max_market) // target.buy_price if target.buy_price > 0 else 0
            qty = min(max_by_budget, self._cfg.max_contracts, 50)
            if qty < 1:
                continue

            order = self._place_buy(target, qty)
            if order and order.state != OrderState.ERROR:
                self._positions[target.ticker] = MMPosition(
                    ticker=target.ticker,
                    side="yes",
                    buy_order=order,
                )
                remaining -= qty * target.buy_price
                placed += 1
                logger.info(
                    "MM BUY placed: %s  price=%d¢  qty=%d  spread=%d  target_sell=%d¢  "
                    "expected_profit=%d¢/ea",
                    target.ticker, target.buy_price, qty,
                    target.spread, target.sell_price, target.profit_per_rt,
                )
            time.sleep(0.3)

        return placed

    def check_fills_and_manage(self) -> List[RoundTrip]:
        """Check pending orders, manage inventory, place sells.

        This should be called frequently (every few seconds).
        Returns any completed round-trips.
        """
        completed: List[RoundTrip] = []
        tickers_to_remove = []

        for ticker, pos in list(self._positions.items()):
            # ── Check buy order status ────────────────────────────────
            if pos.buy_order and pos.buy_order.state == OrderState.PENDING:
                filled = self._check_order_fill(pos.buy_order)
                if filled:
                    pos.quantity = pos.buy_order.filled_qty
                    pos.avg_cost = pos.buy_order.price
                    pos.buy_order.state = OrderState.FILLED
                    pos.buy_order.filled_at = time.time()
                    self._total_bought += pos.quantity
                    logger.info(
                        "MM BUY FILLED: %s  %d@%d¢",
                        ticker, pos.quantity, pos.avg_cost,
                    )
                    # Immediately place sell
                    self._place_sell_for_position(pos)
                elif self._buy_timed_out(pos.buy_order):
                    # Cancel stale buy
                    self._cancel_order(pos.buy_order)
                    tickers_to_remove.append(ticker)
                    logger.info("MM BUY TIMEOUT: %s (cancelled)", ticker)
                    continue

            # ── Check sell order status ────────────────────────────────
            if pos.sell_order and pos.sell_order.state == OrderState.PENDING:
                filled = self._check_order_fill(pos.sell_order)
                if filled:
                    pos.sell_order.state = OrderState.FILLED
                    self._total_sold += pos.sell_order.filled_qty

                    # Record completed round-trip
                    rt = RoundTrip(
                        ticker=ticker,
                        buy_price=pos.avg_cost,
                        sell_price=pos.sell_order.price,
                        quantity=pos.sell_order.filled_qty,
                        profit_cents=(pos.sell_order.price - pos.avg_cost) * pos.sell_order.filled_qty,
                        buy_fee=0,  # maker
                        sell_fee=0,  # maker
                        net_profit=(pos.sell_order.price - pos.avg_cost) * pos.sell_order.filled_qty,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._round_trips.append(rt)
                    completed.append(rt)
                    tickers_to_remove.append(ticker)
                    logger.info(
                        "MM ROUND TRIP: %s  buy=%d¢ sell=%d¢  qty=%d  profit=%+d¢",
                        ticker, rt.buy_price, rt.sell_price, rt.quantity, rt.net_profit,
                    )

            # ── Stop loss check ───────────────────────────────────────
            if pos.quantity > 0 and not pos.sell_order:
                # Position has inventory but no sell order — shouldn't happen
                self._place_sell_for_position(pos)
            elif pos.quantity > 0 and pos.sell_order and pos.sell_order.state == OrderState.PENDING:
                # Check if we need emergency stop-loss
                bid, ask = self._get_live_quote(ticker)
                if bid > 0 and bid < pos.avg_cost - self._stop_loss_cents:
                    # Cancel maker sell and do taker sell
                    logger.warning(
                        "MM STOP LOSS: %s  cost=%d¢  bid=%d¢  loss=%d¢/ea",
                        ticker, pos.avg_cost, bid, pos.avg_cost - bid,
                    )
                    self._cancel_order(pos.sell_order)
                    self._emergency_sell(pos, bid)
                    rt = RoundTrip(
                        ticker=ticker,
                        buy_price=pos.avg_cost,
                        sell_price=bid,
                        quantity=pos.quantity,
                        profit_cents=(bid - pos.avg_cost) * pos.quantity,
                        buy_fee=0,
                        sell_fee=TAKER_FEE * pos.quantity,
                        net_profit=(bid - pos.avg_cost - TAKER_FEE) * pos.quantity,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._round_trips.append(rt)
                    completed.append(rt)
                    tickers_to_remove.append(ticker)

        # Clean up completed positions
        for tk in tickers_to_remove:
            self._positions.pop(tk, None)

        return completed

    @property
    def session_stats(self) -> dict:
        """Session performance metrics."""
        elapsed = time.time() - self._session_start
        hours = elapsed / 3600 if elapsed > 0 else 0.001
        total_profit = sum(rt.net_profit for rt in self._round_trips)
        wins = sum(1 for rt in self._round_trips if rt.net_profit > 0)
        losses = sum(1 for rt in self._round_trips if rt.net_profit <= 0)
        return {
            "round_trips": len(self._round_trips),
            "total_profit_cents": total_profit,
            "total_profit_dollars": total_profit / 100,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / max(1, wins + losses),
            "cents_per_hour": total_profit / hours if hours > 0 else 0,
            "dollars_per_hour": total_profit / 100 / hours if hours > 0 else 0,
            "active_positions": len(self._positions),
            "total_bought": self._total_bought,
            "total_sold": self._total_sold,
            "elapsed_hours": hours,
        }

    @property
    def active_positions(self) -> Dict[str, MMPosition]:
        return dict(self._positions)

    # ── Order placement ───────────────────────────────────────────────────────

    def _place_buy(self, target: SpreadTarget, qty: int) -> Optional[MMOrder]:
        """Place a limit buy order at the target's buy_price."""
        order = MMOrder(
            ticker=target.ticker,
            side="yes",
            action="buy",
            price=target.buy_price,
            quantity=qty,
            placed_at=time.time(),
        )

        if self._cfg.dry_run:
            # In dry run, simulate immediate fill
            order.order_id = f"dry_{uuid.uuid4().hex[:8]}"
            order.state = OrderState.FILLED
            order.filled_qty = qty
            order.filled_at = time.time()
            return order

        if not self._client:
            order.state = OrderState.ERROR
            order.error = "No trading client"
            return order

        try:
            resp = self._client.create_order(
                ticker=target.ticker,
                action="buy",
                side="yes",
                count=qty,
                order_type="limit",
                yes_price=target.buy_price,
            )
            o = resp.get('order', resp)
            order.order_id = o.get('order_id', '')
            # Check if it filled immediately (taker)
            status = o.get('status', '')
            if status == 'executed':
                order.state = OrderState.FILLED
                order.filled_qty = qty
                order.filled_at = time.time()
            else:
                order.state = OrderState.PENDING
                order.filled_qty = o.get('fill_count', 0)
            return order
        except KalshiAPIError as e:
            logger.error("MM buy order failed: %s", e)
            order.state = OrderState.ERROR
            order.error = str(e)
            return order

    def _place_sell_for_position(self, pos: MMPosition) -> None:
        """Place a limit sell at the spread target's sell price."""
        if pos.quantity <= 0:
            return

        # Re-fetch current ask to set sell price
        bid, ask = self._get_live_quote(pos.ticker)
        if ask <= 0:
            ask = pos.avg_cost + 2  # fallback: cost + 2¢

        # Set sell price: at current ask (or ask-1 if spread is wide)
        spread = ask - bid if bid > 0 else 2
        if spread >= 4:
            sell_price = ask - 1
        else:
            sell_price = ask
        # Ensure we don't sell below cost
        sell_price = max(sell_price, pos.avg_cost + 1)

        order = MMOrder(
            ticker=pos.ticker,
            side="yes",
            action="sell",
            price=sell_price,
            quantity=pos.quantity,
            placed_at=time.time(),
        )

        if self._cfg.dry_run:
            order.order_id = f"dry_{uuid.uuid4().hex[:8]}"
            order.state = OrderState.FILLED
            order.filled_qty = pos.quantity
            order.filled_at = time.time()
            pos.sell_order = order
            return

        if not self._client:
            return

        try:
            resp = self._client.create_order(
                ticker=pos.ticker,
                action="sell",
                side="yes",
                count=pos.quantity,
                order_type="limit",
                yes_price=sell_price,
            )
            o = resp.get('order', resp)
            order.order_id = o.get('order_id', '')
            status = o.get('status', '')
            if status == 'executed':
                order.state = OrderState.FILLED
                order.filled_qty = pos.quantity
                order.filled_at = time.time()
            else:
                order.state = OrderState.PENDING
                order.filled_qty = o.get('fill_count', 0)
            pos.sell_order = order
            logger.info(
                "MM SELL placed: %s  price=%d¢  qty=%d  (cost=%d¢, spread=%d¢)",
                pos.ticker, sell_price, pos.quantity, pos.avg_cost, sell_price - pos.avg_cost,
            )
        except KalshiAPIError as e:
            logger.error("MM sell order failed for %s: %s", pos.ticker, e)
            order.state = OrderState.ERROR
            order.error = str(e)

    def _emergency_sell(self, pos: MMPosition, bid: int) -> None:
        """Taker sell at market bid for stop loss."""
        if self._cfg.dry_run or not self._client:
            return
        try:
            self._client.create_order(
                ticker=pos.ticker,
                action="sell",
                side="yes",
                count=pos.quantity,
                order_type="limit",
                yes_price=max(1, bid),
            )
        except Exception as e:
            logger.error("Emergency sell failed for %s: %s", pos.ticker, e)

    # ── Order monitoring ──────────────────────────────────────────────────────

    def _check_order_fill(self, order: MMOrder) -> bool:
        """Check if a pending order has been filled."""
        if self._cfg.dry_run:
            return order.state == OrderState.FILLED

        if not order.order_id or not self._data:
            return False

        try:
            # Check fills for this ticker
            fills = self._data.get_fills(ticker=order.ticker, limit=20)
            fill_list = fills.get('fills', [])
            # Count fills matching our order
            filled = 0
            for f in fill_list:
                if f.get('order_id') == order.order_id:
                    filled += f.get('count', 0)
            if filled >= order.quantity:
                order.filled_qty = filled
                return True
            elif filled > 0:
                order.filled_qty = filled
                order.state = OrderState.PARTIAL
            return False
        except Exception as e:
            logger.debug("Fill check error for %s: %s", order.ticker, e)
            return False

    def _buy_timed_out(self, order: MMOrder) -> bool:
        """Check if a buy order has exceeded the timeout."""
        if order.state != OrderState.PENDING:
            return False
        return (time.time() - order.placed_at) > self._buy_timeout

    def _cancel_order(self, order: MMOrder) -> None:
        """Cancel a pending order."""
        if self._cfg.dry_run or not self._client:
            order.state = OrderState.CANCELLED
            return
        if not order.order_id:
            return
        try:
            self._client.cancel_order(order.order_id)
            order.state = OrderState.CANCELLED
        except Exception as e:
            logger.debug("Cancel failed for %s: %s", order.order_id, e)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_available_balance(self) -> int:
        """Get available cash balance in cents."""
        if self._cfg.dry_run:
            return 99999
        try:
            bal = self._data.get_balance()
            return int(bal.get('balance', 0))
        except Exception:
            return 0

    def _get_live_quote(self, ticker: str) -> Tuple[int, int]:
        """Get current (bid, ask) for a ticker."""
        try:
            data = self._data.get_market(ticker)
            m = data.get('market', data)
            return (m.get('yes_bid', 0) or 0, m.get('yes_ask', 0) or 0)
        except Exception:
            return (0, 0)
