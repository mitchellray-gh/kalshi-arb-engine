"""
engine/trading_engine.py — Rapid arb + profit engine.

DUAL-SPEED LOOP:
  Fast cycle (every PROFIT_CHECK_SEC):  check positions, take profits, trail stops
  Slow cycle (every SCAN_INTERVAL_SEC): full market scan → detect arbs → execute

Capital is recycled immediately — profits from exits are redeployed on the
next scan. This compounding effect is the core wealth-building mechanism.

Strategies:
  BUY-ALL-YES: sum(yes_ask) + fees < 100¢ → guaranteed profit at settlement
  BUY-ALL-NO:  sum(yes_bid) - fees > 100¢ → guaranteed profit at settlement
  SCALP:       exit early at small profit to free capital for next rotation
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
from .profit_taker import ProfitTaker
from .scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketScanner

logger = logging.getLogger(__name__)


class TradingEngine:
    """Dual-speed arb + profit-taking engine for rapid wealth building."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

        if cfg.dry_run and (not cfg.api_key_id or not cfg.private_key_path
                           or not os.path.isfile(cfg.private_key_path)):
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
            self._profit_taker = ProfitTaker(
                client=self._client,
                data_client=self._data_client,
                cfg=cfg,
            )

        self._store = PositionStore()
        self._cycle_count = 0

        # Dual-speed timing
        self._profit_check_sec = getattr(cfg, 'profit_check_seconds', 5)
        self._scan_interval_sec = cfg.scan_interval_seconds
        # How many fast cycles between full scans
        self._scan_every_n = max(1, self._scan_interval_sec // self._profit_check_sec)

    def run(self) -> None:
        """Run the dual-speed engine loop forever."""
        cfg = self._cfg

        if not self._scanner:
            raise RuntimeError(
                "Cannot run engine: no API credentials configured.\n"
                "1. Copy .env.example → .env\n"
                "2. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n"
                "3. Generate keys at: https://kalshi.com/account/profile → API Keys"
            )

        print("\n" + "=" * 72)
        print("  KALSHI RAPID ARBITRAGE + SCALP ENGINE")
        print("=" * 72)
        print(f"  env={cfg.env}  dry_run={cfg.dry_run}")
        print(f"  min_profit={cfg.min_profit_cents}¢  max_order={cfg.max_order_cents}¢  "
              f"max_contracts={cfg.max_contracts}")
        print(f"  PROFIT CHECK: every {self._profit_check_sec}s  "
              f"FULL SCAN: every {self._scan_interval_sec}s "
              f"(1 per {self._scan_every_n} cycles)")
        scalp = getattr(cfg, 'min_scalp_cents', 1)
        trail = getattr(cfg, 'trail_stop_cents', 3)
        print(f"  scalp_min={scalp}¢  trail_stop={trail}¢  "
              f"stop_loss={cfg.stop_loss_pct:.0%}")
        print(f"  STRATEGY: Arb + rapid profit-take → compound capital")
        print("=" * 72 + "\n")

        while True:
            try:
                self._fast_cycle()
            except KeyboardInterrupt:
                self._print_session_summary()
                raise
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            time.sleep(self._profit_check_sec)

    def run_once(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        """Run a single full scan+execute cycle (for testing)."""
        self._run_profit_taker()
        return self._run_scanner()

    # ── Dual-speed cycle ──────────────────────────────────────────────────────

    def _fast_cycle(self) -> None:
        """
        Fast cycle runs every PROFIT_CHECK_SEC.
        Always: check profits.
        Every Nth cycle: full scan + execute.
        """
        self._cycle_count += 1
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # ALWAYS: Check profit-taking + stop-loss (fast — just reads positions)
        self._run_profit_taker()

        # EVERY Nth cycle: Full market scan + arb execution
        if self._cycle_count % self._scan_every_n == 0:
            self._check_settlements()
            executions = self._run_scanner()
            if executions:
                placed = sum(1 for e in executions if e.success)
                total_profit = sum(e.total_profit for e in executions if e.success)
                print(f"  [{ts}] SCAN: {placed} arb(s) placed  |  "
                      f"locked profit: {total_profit}¢ (${total_profit/100:.2f})")

        # Periodic wealth status (every 20 cycles ≈ every ~100s)
        if self._cycle_count % 20 == 0:
            self._print_wealth_status()

    # ── Arb scanner + executor ────────────────────────────────────────────────

    def _run_scanner(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        """Full market scan + execute detected arbs."""
        cfg = self._cfg
        scan = self._scanner.scan()

        if not scan.event_arbs and not scan.single_arbs:
            return []

        executions: list[Union[EventArbExecution, SingleArbExecution]] = []

        # Execute event arbs — one at a time with balance check
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

        # Execute single-market arbs (rare)
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

    # ── Profit taker ──────────────────────────────────────────────────────────

    def _run_profit_taker(self) -> None:
        """Fast-cycle profit check: scalp, trail, stop-loss."""
        if not hasattr(self, '_profit_taker') or not self._profit_taker:
            return
        try:
            results = self._profit_taker.check_and_sell()
            if results:
                sold = [r for r in results if r.status in ("sold", "dry_run")]
                if sold:
                    total_realized = sum(r.realized_pnl for r in sold)
                    reasons = {}
                    for r in sold:
                        reasons[r.reason] = reasons.get(r.reason, 0) + 1
                    reason_str = ", ".join(f"{v}×{k}" for k, v in reasons.items())
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] PROFIT: {len(sold)} exit(s)  |  "
                          f"realized: {total_realized:+d}¢ (${total_realized/100:+.2f})  "
                          f"[{reason_str}]")
        except Exception as exc:
            logger.error("Profit taker error: %s", exc, exc_info=True)

    # ── Wealth tracking ───────────────────────────────────────────────────────

    def _print_wealth_status(self) -> None:
        """Periodic wealth dashboard."""
        if not hasattr(self, '_profit_taker'):
            return

        stats = self._profit_taker.session_stats
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

        # Also get live balance if possible
        balance_str = ""
        if self._data_client:
            try:
                bal = self._data_client.get_balance()
                cash = bal.get("balance", 0)
                port = bal.get("portfolio_value", 0)
                balance_str = f"  cash={cash}¢ (${cash/100:.2f})  portfolio={port}¢ (${port/100:.2f})"
            except Exception:
                pass

        if stats['wins'] > 0 or stats['losses'] > 0:
            print(f"\n  [{ts}] WEALTH STATUS"
                  f"  session_pnl={stats['realized_cents']:+d}¢ (${stats['realized_dollars']:+.2f})"
                  f"  wins={stats['wins']}  losses={stats['losses']}"
                  f"  rate=${stats['dollars_per_hour']:+.2f}/hr"
                  f"{balance_str}\n")
        elif balance_str:
            print(f"  [{ts}] STATUS:{balance_str}")

    def _print_session_summary(self) -> None:
        """Print final session summary on shutdown."""
        if not hasattr(self, '_profit_taker'):
            return

        stats = self._profit_taker.session_stats
        print("\n" + "=" * 72)
        print("  SESSION SUMMARY")
        print("=" * 72)
        print(f"  Duration:     {stats['elapsed_hours']:.1f} hours")
        print(f"  Total P&L:    {stats['realized_cents']:+d}¢ (${stats['realized_dollars']:+.2f})")
        print(f"  Wins:         {stats['wins']}")
        print(f"  Losses:       {stats['losses']}")
        print(f"  Rate:         ${stats['dollars_per_hour']:+.2f}/hour")
        print(f"  Cap freed:    {stats['capital_freed_cents']}¢ (${stats['capital_freed_cents']/100:.2f})")
        print("=" * 72 + "\n")
