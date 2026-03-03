"""
engine/positions.py — Track open arb positions and detect settlement.

Each position = one YES+NO pair on a single market.
When the market settles, one side pays 100¢, the other pays 0¢.
We collect 100¢ and our cost was yes_ask + no_ask + fees → locked profit.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

RESULTS_DIR = "results"
RESULTS_FILE = os.path.join(RESULTS_DIR, "positions.json")


@dataclass
class ArbPosition:
    ticker:         str
    title:          str
    yes_price:      int        # cents paid for YES
    no_price:       int        # cents paid for NO
    quantity:       int
    fee_total:      int        # total fees in cents
    locked_profit:  int        # cents per pair
    total_cost:     int        # total cents deployed
    total_profit:   int        # expected total cents profit
    opened_at:      str = ""
    settled_at:     str = ""
    status:         str = "open"   # "open", "won", "void"
    result:         str = ""       # "yes", "no", "void", ""
    actual_pnl:     int = 0        # actual cents P&L after settlement

    @property
    def cost_dollars(self) -> float:
        return self.total_cost / 100.0

    @property
    def profit_dollars(self) -> float:
        return self.total_profit / 100.0


class PositionStore:
    """Persistent JSON store for arb positions."""

    def __init__(self, path: str = RESULTS_FILE) -> None:
        self._path = path
        self._positions: list[ArbPosition] = []
        self._load()

    def _load(self) -> None:
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r") as f:
                    data = json.load(f)
                self._positions = [ArbPosition(**p) for p in data]
                logger.info("Loaded %d positions from %s", len(self._positions), self._path)
            except Exception as e:
                logger.warning("Failed to load positions: %s", e)
                self._positions = []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump([asdict(p) for p in self._positions], f, indent=2)

    def add(self, pos: ArbPosition) -> None:
        self._positions.append(pos)
        self.save()

    def all(self) -> list[ArbPosition]:
        return list(self._positions)

    def open_positions(self) -> list[ArbPosition]:
        return [p for p in self._positions if p.status == "open"]

    def count_open(self) -> int:
        return sum(1 for p in self._positions if p.status == "open")

    def has_ticker(self, ticker: str) -> bool:
        return any(p.ticker == ticker and p.status == "open" for p in self._positions)

    def settle(self, ticker: str, result: str) -> None:
        """Mark a position as settled."""
        for p in self._positions:
            if p.ticker == ticker and p.status == "open":
                p.status = "won" if result in ("yes", "no") else "void"
                p.result = result
                p.settled_at = datetime.now(timezone.utc).isoformat()
                if result in ("yes", "no"):
                    # One side pays 100¢ → profit = 100*qty - total_cost
                    p.actual_pnl = 100 * p.quantity - p.total_cost
                elif result == "void":
                    # Refunded at cost, but fees are lost
                    p.actual_pnl = -p.fee_total
                logger.info(
                    "SETTLED %s  result=%s  actual_pnl=%d¢ ($%.2f)",
                    ticker, result, p.actual_pnl, p.actual_pnl / 100,
                )
        self.save()
