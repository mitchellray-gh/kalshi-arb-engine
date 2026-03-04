"""
main.py - Kalshi Daily Profit Engine
-------------------------------------
Usage:
  python main.py                          # start dual-strategy engine (MM + arb)
  python main.py --scan                   # one-shot arb scan, print signals, exit
  python main.py --spread-scan            # one-shot spread scan, show MM targets
  python main.py --mm-status              # show market-maker session stats
  python main.py --estimate               # estimate profit from live market data
  python main.py --sim-returns            # Monte Carlo simulate returns
  python main.py --report                 # print P&L report
  python main.py --balance                # show account balance
  python main.py --positions              # show live positions with current P&L
  python main.py --dry-run                # force DRY_RUN=true for this session
  python main.py --env prod               # use production API (default: demo)

Strategies:
  1. SPREAD CAPTURE (primary): Market-make on high-volume sports markets.
     Place maker limit buys (0c fee), sell at higher price (0c fee).
     Capture 1-2c per round-trip. Daily profit from same-day settlements.

  2. MULTI-OUTCOME ARB (secondary): BUY ALL YES/NO on ME events where
     sum(asks) + fees < 100c. Guaranteed profit at settlement.
"""
from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from engine.config import load_config
from engine.logger_setup import setup_logging


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Kalshi Daily Profit Engine (spread capture + arb)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Strategy commands
    p.add_argument("--scan",         action="store_true",
                   help="One-shot arb scan, print signals, exit.")
    p.add_argument("--spread-scan",  action="store_true",
                   help="One-shot spread scan, show market-making targets.")
    p.add_argument("--weather-scan", action="store_true",
                   help="NOAA weather edge scan (shows edges, no orders).")
    p.add_argument("--weather-trade", action="store_true",
                   help="Weather scan + place orders on high-edge contracts.")
    p.add_argument("--mm-status",    action="store_true",
                   help="Show market-maker session stats and active positions.")
    # Analysis
    p.add_argument("--estimate",     action="store_true",
                   help="Estimate profit from live market data (no auth required).")
    p.add_argument("--sim-returns",  action="store_true",
                   help="Monte Carlo simulate returns from live market data.")
    p.add_argument("--report",       action="store_true",
                   help="Print P&L report from results/positions.json.")
    p.add_argument("--balance",      action="store_true",
                   help="Show Kalshi account balance.")
    p.add_argument("--positions",    action="store_true",
                   help="Show live positions with current P&L.")
    # Config overrides
    p.add_argument("--dry-run",      action="store_true", default=None,
                   help="Override DRY_RUN=true (no real orders).")
    p.add_argument("--env",          type=str, default=None,
                   choices=["demo", "prod"],
                   help="API environment (default: from .env or demo).")
    # Simulation params
    p.add_argument("--sim-daily",    action="store_true",
                   help="Simulate daily P&L if engine ran non-stop (uses live data).")
    p.add_argument("--days",         type=int, default=30,
                   help="Number of days to simulate for --sim-daily (default: 30).")
    p.add_argument("--capital",      type=float, default=500.0,
                   help="Starting capital in dollars for --sim-daily (default: 500).")
    p.add_argument("--trials",       type=int, default=1_000,
                   help="Number of Monte Carlo trials (default: 1000).")
    p.add_argument("--seed",         type=int, default=None,
                   help="RNG seed for reproducible simulation runs.")
    return p.parse_args()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(cfg) -> None:
    """One-shot scan: find arb signals and print them."""
    cfg.validate()
    from engine.client import KalshiClient
    from engine.scanner import MarketScanner
    client = KalshiClient(cfg)
    scanner = MarketScanner(client, cfg)
    result = scanner.scan()

    print(f"\n  Scanned {result.me_events} ME events, {result.total_markets} markets "
          f"in {result.elapsed_ms:.0f}ms")
    print(f"  Event arbs: {len(result.event_arbs)}  |  Single arbs: {len(result.single_arbs)}\n")

    if not result.event_arbs and not result.single_arbs:
        print("  No arb signals found. Markets are efficiently priced right now.")
        print("  The engine scans continuously to catch transient mispricings.\n")
        return

    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    if result.event_arbs:
        rows = []
        for ea in result.event_arbs:
            liquid = "YES" if ea.is_liquid else f"NO ({ea.dead_legs} dead)"
            rows.append([
                ea.event_ticker[:30],
                ea.arb_type.replace("_", " ").upper(),
                ea.n_markets,
                f"{ea.sum_ask}¢",
                f"{ea.profit_per_set}¢",
                f"{ea.roi_per_set:+.0%}",
                f"{ea.exit_recovery_pct:.0f}%",
                liquid,
            ])
        hdrs = ["Event", "Strategy", "Legs", "Cost", "Profit", "ROI", "Exit%", "Liquid"]
        if tab:
            print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
        else:
            for r in rows:
                print("  " + "  ".join(str(c) for c in r))

    if result.single_arbs:
        rows = []
        for m in result.single_arbs:
            rows.append([
                m.ticker[:35],
                f"{m.yes_ask}¢",
                f"{m.no_ask}¢",
                f"{m.locked_profit_cents}¢",
                m.volume_24h,
            ])
        hdrs = ["Ticker", "YES", "NO", "Profit", "Vol24h"]
        if tab:
            print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
        else:
            for r in rows:
                print("  " + "  ".join(str(c) for c in r))
    print()


def cmd_estimate(cfg) -> None:
    """Estimate profit from live market data (no auth needed)."""
    from estimate import run_estimate
    run_estimate(cfg)


def cmd_sim_returns(cfg, n_trials: int = 1_000, seed: int | None = None) -> None:
    """Monte Carlo simulate resolution of arb positions."""
    from estimate import run_simulation
    run_simulation(cfg, n_trials=n_trials, seed=seed)


def cmd_sim_daily(
    cfg, n_days: int = 30, n_trials: int = 1_000,
    seed: int | None = None, capital: float = 500.0,
) -> None:
    """Simulate daily P&L if engine ran non-stop."""
    from estimate import run_daily_sim
    run_daily_sim(cfg, n_days=n_days, n_trials=n_trials, seed=seed,
                  capital_dollars=capital)


def cmd_report() -> None:
    """Print P&L report from stored positions."""
    from engine.positions import PositionStore
    from engine.report import print_report
    store = PositionStore()
    print_report(store.all())


def cmd_balance(cfg) -> None:
    """Show Kalshi account balance."""
    cfg.validate()
    from engine.client import KalshiClient
    client = KalshiClient(cfg)
    bal = client.get_balance()
    balance = bal.get("balance", 0)
    portfolio = bal.get("portfolio_value", 0)
    print(f"\n  Balance        : {balance}¢  (${balance/100:,.2f})")
    print(f"  Portfolio value: {portfolio}¢  (${portfolio/100:,.2f})")
    print(f"  Total          : {balance + portfolio}¢  (${(balance + portfolio)/100:,.2f})\n")


def cmd_positions(cfg) -> None:
    """Show live positions with current market prices, P&L, and scalp readiness."""
    cfg.validate()
    from engine.client import KalshiClient
    from engine.profit_taker import ProfitTaker, ROUND_TRIP_FEE
    data_client = KalshiClient(cfg)
    pt = ProfitTaker(client=None, data_client=data_client, cfg=cfg)
    snapshots = pt.get_position_report()

    if not snapshots:
        print("\n  No open positions.\n")
        return

    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    rows = []
    total_cost = 0
    total_pnl = 0
    scalp_ready = 0
    min_scalp = getattr(cfg, 'min_scalp_cents', 1)

    for s in snapshots:
        cost_total = (s.cost_basis_cents + 2) * s.quantity  # include buy fee
        total_cost += cost_total
        total_pnl += s.pnl_if_sell

        # Check if scalp-ready
        net = s.net_per_contract
        ready = "SCALP" if net >= min_scalp else ("HOLD" if net > -ROUND_TRIP_FEE else "LOSS")
        if net >= min_scalp:
            scalp_ready += 1

        rows.append([
            s.ticker[:35],
            s.side.upper(),
            s.quantity,
            f"{s.cost_basis_cents}¢",
            f"{s.current_bid}¢",
            f"{net:+d}¢",
            f"{s.pnl_pct:+.0%}",
            ready,
        ])

    hdrs = ["Market", "Side", "Qty", "Cost", "Bid", "Net/ea", "ROI", "Status"]
    print()
    if tab:
        print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    else:
        print("  ".join(hdrs))
        for r in rows:
            print("  " + "  ".join(str(c) for c in r))

    print(f"\n  Total invested:        {total_cost}¢ (${total_cost/100:.2f})")
    print(f"  Total P&L if sold now: {total_pnl:+d}¢ (${total_pnl/100:+.2f})")
    print(f"  Scalp-ready positions: {scalp_ready}/{len(snapshots)}")
    print(f"  Min scalp threshold:   {min_scalp}¢ net profit/contract")
    print(f"  Round-trip fee:        {ROUND_TRIP_FEE}¢ (2¢ buy + 2¢ sell)")
    print(f"  Trail stop distance:   {getattr(cfg, 'trail_stop_cents', 3)}¢")
    print(f"  Stop loss threshold:   {cfg.stop_loss_pct:.0%} of cost\n")


def cmd_run(cfg) -> None:
    """Start the live arb engine loop."""
    cfg.validate()
    from engine.trading_engine import TradingEngine
    TradingEngine(cfg).run()


def cmd_spread_scan(cfg) -> None:
    """One-shot spread scan: find market-making targets."""
    cfg.validate()
    from engine.client import KalshiClient
    from engine.spread_scanner import SpreadScanner

    client = KalshiClient(cfg)
    scanner = SpreadScanner(client, cfg)
    result = scanner.scan()

    print(f"\n  Scanned {result.total_scanned} markets  |  "
          f"{result.two_sided} two-sided  |  "
          f"{result.elapsed_ms:.0f}ms")
    print(f"  Spread targets found: {len(result.targets)}\n")

    if not result.targets:
        print("  No spread targets found. Markets may be too tight or volume too low.")
        print(f"  Config: min_spread={cfg.mm_min_spread}c  min_volume={cfg.mm_min_volume}\n")
        return

    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    rows = []
    for t in result.targets[:30]:
        rows.append([
            t.ticker[:35],
            f"{t.yes_bid}c",
            f"{t.yes_ask}c",
            f"{t.spread}c",
            f"{t.buy_price}c",
            f"{t.sell_price}c",
            f"{t.profit_per_rt}c",
            f"{t.volume_24h:,}",
            f"{t.score:.0f}",
        ])

    hdrs = ["Market", "Bid", "Ask", "Spread", "Buy@", "Sell@", "Profit/RT", "Vol24h", "Score"]
    if tab:
        print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    else:
        print("  ".join(hdrs))
        for r in rows:
            print("  " + "  ".join(str(c) for c in r))
    print(f"\n  Top target: {result.targets[0].ticker}")
    print(f"    Buy at {result.targets[0].buy_price}c (maker, 0c fee) -> "
          f"Sell at {result.targets[0].sell_price}c (maker, 0c fee) = "
          f"{result.targets[0].profit_per_rt}c profit/contract\n")


def cmd_weather_scan(cfg, trade: bool = False) -> None:
    """Scan weather markets for NOAA-data edges. Optionally place trades."""
    cfg.validate()
    from engine.client import KalshiClient
    from engine.weather_trader import WeatherTrader

    client = KalshiClient(cfg)
    trader = WeatherTrader(client, cfg)

    print("\n  Fetching NOAA forecasts and scanning Kalshi weather markets...")
    signal = trader.scan(
        max_days_out=cfg.wx_max_days_out,
        sigma=cfg.wx_sigma,
        min_edge=cfg.wx_min_edge,
        min_volume=cfg.wx_min_volume,
    )
    trader.print_scan_report(signal)

    if trade and signal.edges:
        dry = cfg.dry_run
        print(f"  {'[DRY RUN] ' if dry else ''}Executing trades on {len(signal.edges)} edges...\n")
        orders = trader.execute_trades(
            signal,
            max_contracts_per_trade=cfg.wx_max_contracts,
            max_total_spend_cents=cfg.wx_max_bet_cents,
            dry_run=dry,
        )
        print(f"  Orders placed: {len(orders)}")
        total_ev = sum(o.get('ev_cents', 0) for o in orders)
        total_cost = sum(o.get('count', 0) * o.get('price', 0) for o in orders)
        print(f"  Total cost: ${total_cost/100:.2f}")
        print(f"  Total expected value: ${total_ev/100:+.2f}")
        print(f"  Settlement: next day\n")


def cmd_mm_status(cfg) -> None:
    """Show market-maker status (requires running engine instance or saved state)."""
    print("\n  Market Maker Status")
    print("  " + "-" * 40)
    print("  The market maker runs as part of the engine.")
    print("  Start the engine with: python main.py --env prod")
    print("  Stats are displayed live during engine operation.")
    print()
    # Show config for reference
    print("  Current MM Config:")
    print(f"    mm_enabled:       {getattr(cfg, 'mm_enabled', True)}")
    print(f"    mm_min_spread:    {cfg.mm_min_spread}c")
    print(f"    mm_min_volume:    {cfg.mm_min_volume}")
    print(f"    mm_buy_timeout:   {cfg.mm_buy_timeout}s")
    print(f"    mm_stop_loss:     {cfg.mm_stop_loss}c")
    print(f"    mm_max_per_market:{cfg.mm_max_per_market}%")
    print(f"    mm_max_total:     {cfg.mm_max_total_exposure}%")
    print(f"    mm_check_interval:{cfg.mm_check_interval}s")
    print(f"    mm_scan_interval: {cfg.mm_scan_interval}s")
    print()


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    cfg = load_config()

    # CLI overrides
    if args.dry_run:
        cfg.dry_run = True
    if args.env:
        cfg.env = args.env

    setup_logging(level=cfg.log_level, log_file=cfg.log_file)

    try:
        if args.estimate:
            cmd_estimate(cfg)
        elif args.sim_daily:
            cmd_sim_daily(cfg, n_days=args.days, n_trials=args.trials,
                          seed=args.seed, capital=args.capital)
        elif args.sim_returns:
            cmd_sim_returns(cfg, n_trials=args.trials, seed=args.seed)
        elif args.scan:
            cmd_scan(cfg)
        elif args.spread_scan:
            cmd_spread_scan(cfg)
        elif args.weather_scan:
            cmd_weather_scan(cfg, trade=False)
        elif args.weather_trade:
            cmd_weather_scan(cfg, trade=True)
        elif args.mm_status:
            cmd_mm_status(cfg)
        elif args.report:
            cmd_report()
        elif args.balance:
            cmd_balance(cfg)
        elif args.positions:
            cmd_positions(cfg)
        else:
            cmd_run(cfg)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
