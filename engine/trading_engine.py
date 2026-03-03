"""
engine/trading_engine.py — Main loop: scan → detect arb → execute → settle.

Single strategy: YES + NO sum arbitrage.
  When yes_ask + no_ask + fees < 100¢ → BUY BOTH → guaranteed profit.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List

from .client import KalshiClient
from .config import Config
from .executor import ArbExecution, ArbExecutor
from .positions import ArbPosition, PositionStore
from .scanner import FEE_ROUND_TRIP, MarketScanner, MarketQuote

logger = logging.getLogger(__name__)


class TradingEngine:
    """Scan-detect-execute loop for YES+NO sum arbitrage."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = KalshiClient(cfg) if not cfg.dry_run else None

        # Scanner always needs a client for data
        data_client = KalshiClient(cfg)
        self._scanner = MarketScanner(data_client, cfg)
        self._executor = ArbExecutor(self._client, cfg)
        self._store = PositionStore()

    def run(self) -> None:
        """Run the arb engine loop forever (or until KeyboardInterrupt)."""
        cfg = self._cfg

        print("\n" + "=" * 72)
        print("  KALSHI YES+NO SUM ARBITRAGE ENGINE")
        print(f"  env={cfg.env}  dry_run={cfg.dry_run}")
        print(f"  min_profit={cfg.min_profit_cents}¢  max_order={cfg.max_order_cents}¢  "
              f"max_contracts={cfg.max_contracts}")
        print(f"  scan_interval={cfg.scan_interval_seconds}s")
        print("=" * 72 + "\n")

        while True:
            try:
                self._cycle()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            time.sleep(cfg.scan_interval_seconds)

    def run_once(self) -> List[ArbExecution]:
        """Run a single scan+execute cycle. Returns list of executions."""
        return self._cycle()

    def _cycle(self) -> List[ArbExecution]:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        cfg = self._cfg

        # 1. Settle any expired positions
        self._check_settlements()

        # 2. Scan for arb
        scan = self._scanner.scan()
        if not scan.arb_signals:
            logger.debug("[%s] No arb signals (scanned %d markets)", ts, scan.total_scanned)
            return []

        # 3. Execute arbs (skip if already have position or at max)
        executions: list[ArbExecution] = []
        for mkt in scan.arb_signals:
            if self._store.count_open() >= cfg.max_open_positions:
                logger.info("Max open positions (%d) reached, skipping remaining signals",
                            cfg.max_open_positions)
                break
            if self._store.has_ticker(mkt.ticker):
                logger.debug("Already have open position on %s, skipping", mkt.ticker)
                continue

            result = self._executor.execute(mkt)
            executions.append(result)

            if result.success:
                pos = ArbPosition(
                    ticker=mkt.ticker,
                    title=mkt.title,
                    yes_price=mkt.yes_ask,
                    no_price=mkt.no_ask,
                    quantity=result.quantity,
                    fee_total=FEE_ROUND_TRIP * result.quantity,
                    locked_profit=mkt.locked_profit_cents,
                    total_cost=result.total_cost,
                    total_profit=result.total_profit,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
                self._store.add(pos)

        if executions:
            placed = sum(1 for e in executions if e.success)
            total_profit = sum(e.total_profit for e in executions if e.success)
            print(f"  [{ts}] {placed} arb(s) placed  |  locked profit: {total_profit}¢ (${total_profit/100:.2f})")

        return executions

    def _check_settlements(self) -> None:
        """Check if any open positions have settled."""
        open_pos = self._store.open_positions()
        if not open_pos:
            return

        for pos in open_pos:
            try:
                data = KalshiClient(self._cfg).get_market(pos.ticker)
                mkt = data.get("market", data)
                result = mkt.get("result", "")
                if result in ("yes", "no", "void"):
                    self._store.settle(pos.ticker, result)
            except Exception as exc:
                logger.debug("Cannot check settlement for %s: %s", pos.ticker, exc)
