"""
engine/trading_engine.py — Main loop: scan → detect arb → execute → settle.

Primary: Multi-outcome event arb on mutually-exclusive events.
  BUY-ALL-YES: sum(yes_ask) + fees < 100¢ → buy YES on every outcome.
  BUY-ALL-NO:  sum(yes_bid) - fees > 100¢ → buy NO on every outcome.

Secondary: Single-market sum arb (yes_ask + no_ask < 100¢) — rare.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Union

from .client import KalshiClient
from .config import Config
from .executor import ArbExecutor, EventArbExecution, SingleArbExecution
from .positions import ArbPosition, PositionStore
from .scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketScanner

logger = logging.getLogger(__name__)


class TradingEngine:
    """Scan-detect-execute loop for multi-outcome + single-market arb."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

        if cfg.dry_run and (not cfg.api_key_id or not cfg.private_key_path
                           or not os.path.isfile(cfg.private_key_path)):
            # Dry-run without credentials — scanner can't auth-scan
            # but estimate.py fetch (no-auth) still works via --estimate
            logger.warning(
                "Dry-run mode with no valid credentials. "
                "Engine loop requires API keys even for read-only scanning."
            )
            self._client = None
            self._data_client = None
            self._scanner = None
            self._executor = ArbExecutor(None, cfg)
        else:
            self._client = KalshiClient(cfg) if not cfg.dry_run else None
            self._data_client = KalshiClient(cfg)
            self._scanner = MarketScanner(self._data_client, cfg)
            self._executor = ArbExecutor(self._client, cfg, data_client=self._data_client)

        self._store = PositionStore()

    def run(self) -> None:
        """Run the arb engine loop forever (or until KeyboardInterrupt)."""
        cfg = self._cfg

        if not self._scanner:
            raise RuntimeError(
                "Cannot run engine: no API credentials configured.\n"
                "1. Copy .env.example → .env\n"
                "2. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n"
                "3. Generate keys at: https://kalshi.com/account/profile → API Keys"
            )

        print("\n" + "=" * 72)
        print("  KALSHI MULTI-OUTCOME ARBITRAGE ENGINE")
        print(f"  env={cfg.env}  dry_run={cfg.dry_run}")
        print(f"  min_profit={cfg.min_profit_cents}¢  max_order={cfg.max_order_cents}¢  "
              f"max_contracts={cfg.max_contracts}")
        print(f"  scan_interval={cfg.scan_interval_seconds}s")
        print("  Strategies: BUY-ALL-YES, BUY-ALL-NO (ME events)")
        print("=" * 72 + "\n")

        while True:
            try:
                self._cycle()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            time.sleep(cfg.scan_interval_seconds)

    def run_once(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        """Run a single scan+execute cycle."""
        return self._cycle()

    def _cycle(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        cfg = self._cfg

        # 1. Settle any expired positions
        self._check_settlements()

        # 2. Scan for arbs
        scan = self._scanner.scan()

        if not scan.event_arbs and not scan.single_arbs:
            logger.debug(
                "[%s] No arb signals (%d ME events, %d markets, %.0fms)",
                ts, scan.me_events, scan.total_markets, scan.elapsed_ms,
            )
            return []

        executions: list[Union[EventArbExecution, SingleArbExecution]] = []

        # 3. Execute event arbs (primary strategy) — one at a time with balance check
        for opp in scan.event_arbs:
            if self._store.count_open() >= cfg.max_open_positions:
                logger.info("Max open positions (%d) reached", cfg.max_open_positions)
                break
            if self._store.has_ticker(opp.event_ticker):
                logger.debug("Already have position on %s, skipping", opp.event_ticker)
                continue

            # Rate limit between consecutive arb executions
            if executions:
                from .executor import INTER_ARB_DELAY_SEC
                time.sleep(INTER_ARB_DELAY_SEC)

            result = self._executor.execute_event_arb(opp)
            executions.append(result)

            if result.success:
                pos = ArbPosition(
                    ticker=opp.event_ticker,
                    title=opp.event_title,
                    yes_price=opp.sum_ask,
                    no_price=0,
                    quantity=result.quantity,
                    fee_total=opp.fee_total * result.quantity,
                    locked_profit=opp.profit_per_set,
                    total_cost=result.total_cost,
                    total_profit=result.total_profit,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
                self._store.add(pos)

        # 4. Execute single-market arbs (secondary, rare)
        for mkt in scan.single_arbs:
            if self._store.count_open() >= cfg.max_open_positions:
                break
            if self._store.has_ticker(mkt.ticker):
                continue

            result = self._executor.execute_single(mkt)
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
            total_profit = sum(
                e.total_profit for e in executions if e.success
            )
            print(f"  [{ts}] {placed} arb(s) placed  |  locked profit: {total_profit}¢ (${total_profit/100:.2f})")

        return executions

    def _check_settlements(self) -> None:
        """Check if any open positions have settled."""
        open_pos = self._store.open_positions()
        if not open_pos or not self._data_client:
            return

        for pos in open_pos:
            try:
                data = self._data_client.get_market(pos.ticker)
                mkt = data.get("market", data)
                result = mkt.get("result", "")
                if result in ("yes", "no", "void"):
                    self._store.settle(pos.ticker, result)
                    logger.info("Position %s settled: %s", pos.ticker, result)
            except Exception as exc:
                logger.debug("Cannot check settlement for %s: %s", pos.ticker, exc)
