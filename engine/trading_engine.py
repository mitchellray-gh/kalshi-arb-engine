"""
engine/trading_engine.py — Dual-strategy trading engine.

STRATEGY 1 (PRIMARY): Spread-capture market making
  - Find high-volume markets with 2-4¢ spreads (NBA, NHL, NCAA, etc.)
  - Place maker BUY at bid+1 (0¢ fee), sell at ask or ask-1 (0¢ fee)
  - Capture 1-2¢ per round-trip, many times per day
  - Profit realized within minutes/hours, not weeks

STRATEGY 2 (SECONDARY): Multi-outcome ME event arb
  - BUY-ALL-YES or BUY-ALL-NO when sum < 100¢
  - Only on liquid legs (all have bids)
  - Settlement profit = guaranteed but slow

DUAL-SPEED LOOP:
  Fast cycle (every MM_CHECK_INTERVAL): check MM fills, manage inventory
  Medium cycle (every SCAN_INTERVAL):   arb scan + execute
  Slow cycle (every MM_SCAN_INTERVAL):  full spread scan + place new MM orders
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
from .market_maker import MarketMaker
from .positions import ArbPosition, PositionStore
from .profit_taker import ProfitTaker
from .scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketScanner
from .spread_scanner import SpreadScanner

logger = logging.getLogger(__name__)


class TradingEngine:
    """Dual-strategy engine: market making + arb scanner."""

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
            self._spread_scanner = None
            self._executor = ArbExecutor(None, cfg)
            self._market_maker = None
            self._profit_taker = None
        else:
            self._client = KalshiClient(cfg) if not cfg.dry_run else None
            self._data_client = KalshiClient(cfg)
            self._scanner = MarketScanner(self._data_client, cfg)
            self._spread_scanner = SpreadScanner(self._data_client, cfg)
            self._executor = ArbExecutor(self._client, cfg, data_client=self._data_client)
            self._profit_taker = ProfitTaker(
                client=self._client,
                data_client=self._data_client,
                cfg=cfg,
            )
            if getattr(cfg, 'mm_enabled', True):
                self._market_maker = MarketMaker(
                    client=self._client,
                    data_client=self._data_client,
                    cfg=cfg,
                )
            else:
                self._market_maker = None

        self._store = PositionStore()
        self._cycle_count = 0
        self._last_spread_scan = 0.0

        # Timing
        self._fast_interval = getattr(cfg, 'mm_check_interval', 5)
        self._arb_scan_interval = cfg.scan_interval_seconds
        self._spread_scan_interval = getattr(cfg, 'mm_scan_interval', 60)
        self._arb_every_n = max(1, self._arb_scan_interval // self._fast_interval)
        self._spread_every_n = max(1, self._spread_scan_interval // self._fast_interval)

    def run(self) -> None:
        """Run the dual-strategy engine loop forever."""
        cfg = self._cfg

        if not self._data_client:
            raise RuntimeError(
                "Cannot run engine: no API credentials configured.\n"
                "1. Copy .env.example → .env\n"
                "2. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n"
                "3. Generate keys at: https://kalshi.com/account/profile → API Keys"
            )

        mm_on = self._market_maker is not None
        print("\n" + "=" * 72)
        print("  KALSHI DAILY PROFIT ENGINE")
        print("=" * 72)
        print(f"  env={cfg.env}  dry_run={cfg.dry_run}")
        print(f"  STRATEGY 1: {'SPREAD CAPTURE (market making)' if mm_on else 'DISABLED'}")
        if mm_on:
            print(f"    min_spread={cfg.mm_min_spread}¢  min_vol={cfg.mm_min_volume}  "
                  f"buy_timeout={cfg.mm_buy_timeout}s  stop_loss={cfg.mm_stop_loss}¢")
            print(f"    max_per_market={cfg.mm_max_per_market}%  "
                  f"max_total={cfg.mm_max_total_exposure}%  "
                  f"scan_every={self._spread_scan_interval}s  "
                  f"check_every={self._fast_interval}s")
        print(f"  STRATEGY 2: MULTI-OUTCOME ARB")
        print(f"    min_profit={cfg.min_profit_cents}¢  max_order={cfg.max_order_cents}¢  "
              f"max_contracts={cfg.max_contracts}")
        liq = "ON" if getattr(cfg, 'require_liquid_legs', True) else "OFF"
        print(f"    liquidity_filter={liq}  scan_every={self._arb_scan_interval}s")
        print(f"  PROFIT TAKER: scalp={getattr(cfg, 'min_scalp_cents', 1)}¢  "
              f"trail={getattr(cfg, 'trail_stop_cents', 3)}¢  "
              f"stop_loss={cfg.stop_loss_pct:.0%}")
        print("=" * 72 + "\n")

        while True:
            try:
                self._fast_cycle()
            except KeyboardInterrupt:
                self._print_session_summary()
                raise
            except Exception as exc:
                logger.error("Cycle error: %s", exc, exc_info=True)

            time.sleep(self._fast_interval)

    def run_once(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        """Run a single full scan+execute cycle (for testing)."""
        if self._profit_taker:
            self._run_profit_taker()
        if self._market_maker:
            self._run_spread_cycle()
        return self._run_scanner()

    # ── Dual-speed cycle ──────────────────────────────────────────────────────

    def _fast_cycle(self) -> None:
        self._cycle_count += 1

        # ALWAYS: Market maker fill check + inventory management
        if self._market_maker:
            self._run_mm_check()

        # ALWAYS: Profit taker (for arb positions)
        if self._profit_taker:
            self._run_profit_taker()

        # MEDIUM: Arb scanner
        if self._cycle_count % self._arb_every_n == 0:
            self._check_settlements()
            executions = self._run_scanner()
            if executions:
                placed = sum(1 for e in executions if e.success)
                total_profit = sum(e.total_profit for e in executions if e.success)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  [{ts}] ARB: {placed} arb(s) placed  |  "
                      f"locked profit: {total_profit}¢ (${total_profit/100:.2f})")

        # SLOW: Spread scanner (new MM targets)
        if self._market_maker and self._cycle_count % self._spread_every_n == 0:
            self._run_spread_cycle()

        # Periodic status (every 20 fast cycles)
        if self._cycle_count % 20 == 0:
            self._print_wealth_status()

    # ── Market Making ─────────────────────────────────────────────────────────

    def _run_mm_check(self) -> None:
        if not self._market_maker:
            return
        try:
            completed = self._market_maker.check_fills_and_manage()
            if completed:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                for rt in completed:
                    status = "WIN" if rt.net_profit > 0 else "LOSS"
                    print(f"  [{ts}] MM {status}: {rt.ticker[:30]}  "
                          f"buy={rt.buy_price}¢ sell={rt.sell_price}¢  "
                          f"qty={rt.quantity}  pnl={rt.net_profit:+d}¢")
        except Exception as exc:
            logger.error("MM check error: %s", exc, exc_info=True)

    def _run_spread_cycle(self) -> None:
        if not self._market_maker or not self._spread_scanner:
            return
        try:
            scan = self._spread_scanner.scan()
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

            if not scan.targets:
                logger.debug("No spread targets found")
                return

            placed = self._market_maker.place_spread_orders(scan.targets)

            if placed > 0:
                top = scan.targets[0]
                print(f"  [{ts}] SPREAD SCAN: {len(scan.targets)} targets  |  "
                      f"placed {placed} buy(s)  |  "
                      f"top: {top.ticker[:25]} spread={top.spread}¢ vol={top.volume_24h}")

            self._last_spread_scan = time.time()
        except Exception as exc:
            logger.error("Spread scan error: %s", exc, exc_info=True)

    # ── Arb scanner + executor ────────────────────────────────────────────────

    def _run_scanner(self) -> List[Union[EventArbExecution, SingleArbExecution]]:
        if not self._scanner:
            return []
        cfg = self._cfg
        scan = self._scanner.scan()

        if not scan.event_arbs and not scan.single_arbs:
            return []

        executions: list[Union[EventArbExecution, SingleArbExecution]] = []

        for opp in scan.event_arbs:
            if self._store.count_open() >= cfg.max_open_positions:
                break
            if self._store.has_ticker(opp.event_ticker):
                continue

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
        if not self._profit_taker:
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
                    reason_str = ", ".join(f"{v}x{k}" for k, v in reasons.items())
                    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [{ts}] PROFIT: {len(sold)} exit(s)  |  "
                          f"realized: {total_realized:+d}¢ (${total_realized/100:+.2f})  "
                          f"[{reason_str}]")
        except Exception as exc:
            logger.error("Profit taker error: %s", exc, exc_info=True)

    # ── Wealth tracking ───────────────────────────────────────────────────────

    def _print_wealth_status(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        parts = []

        if self._market_maker:
            mm = self._market_maker.session_stats
            if mm['round_trips'] > 0 or mm['active_positions'] > 0:
                parts.append(
                    f"MM: {mm['round_trips']} trips  "
                    f"pnl={mm['total_profit_cents']:+d}¢  "
                    f"active={mm['active_positions']}  "
                    f"rate=${mm['dollars_per_hour']:+.2f}/hr"
                )

        if self._profit_taker:
            pt = self._profit_taker.session_stats
            if pt['wins'] > 0 or pt['losses'] > 0:
                parts.append(
                    f"ARB: pnl={pt['realized_cents']:+d}¢  "
                    f"wins={pt['wins']}  losses={pt['losses']}"
                )

        if self._data_client:
            try:
                bal = self._data_client.get_balance()
                cash = bal.get("balance", 0)
                port = bal.get("portfolio_value", 0)
                parts.append(f"cash={cash}¢  portfolio={port}¢")
            except Exception:
                pass

        if parts:
            print(f"\n  [{ts}] STATUS  " + "  |  ".join(parts) + "\n")

    def _print_session_summary(self) -> None:
        print("\n" + "=" * 72)
        print("  SESSION SUMMARY")
        print("=" * 72)

        if self._market_maker:
            mm = self._market_maker.session_stats
            print(f"\n  MARKET MAKING:")
            print(f"    Round trips:    {mm['round_trips']}")
            print(f"    Total P&L:      {mm['total_profit_cents']:+d}¢ "
                  f"(${mm['total_profit_dollars']:+.2f})")
            print(f"    Wins/Losses:    {mm['wins']}/{mm['losses']}  "
                  f"({mm['win_rate']:.0%} win rate)")
            print(f"    Rate:           ${mm['dollars_per_hour']:+.2f}/hour")
            print(f"    Active pos:     {mm['active_positions']}")

        if self._profit_taker:
            pt = self._profit_taker.session_stats
            print(f"\n  ARB PROFIT TAKER:")
            print(f"    Duration:       {pt['elapsed_hours']:.1f} hours")
            print(f"    Total P&L:      {pt['realized_cents']:+d}¢ "
                  f"(${pt['realized_dollars']:+.2f})")
            print(f"    Wins/Losses:    {pt['wins']}/{pt['losses']}")
            print(f"    Rate:           ${pt['dollars_per_hour']:+.2f}/hour")

        print("=" * 72 + "\n")
