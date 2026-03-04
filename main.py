"""
main.py — Kalshi YES+NO Sum Arbitrage Engine
─────────────────────────────────────────────
Usage:
  python main.py                          # start live arb engine loop (reads .env)
  python main.py --scan                   # one-shot scan, print arb signals, exit
  python main.py --estimate               # estimate profit from live market data (no auth)
  python main.py --sim-returns            # Monte Carlo simulate returns (no auth)
  python main.py --sim-returns --trials 5000 --seed 42  # reproducible simulation
  python main.py --report                 # print P&L report from results/positions.json
  python main.py --balance                # show account balance
  python main.py --dry-run                # force DRY_RUN=true for this session
  python main.py --env prod               # use production API (default: demo)
  python main.py --help                   # show this help

Strategy:
  BUY YES + BUY NO on any market where yes_ask + no_ask + fees < 100¢
  Exactly one side ALWAYS pays 100¢ at settlement → guaranteed profit.
  Only risk: VOID resolution (event cancelled) → fees lost, principal refunded.
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
        description="Kalshi YES+NO sum arbitrage engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--scan",         action="store_true",
                   help="One-shot scan for arb opportunities, print results, exit.")
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
    p.add_argument("--dry-run",      action="store_true", default=None,
                   help="Override DRY_RUN=true (no real orders).")
    p.add_argument("--env",          type=str, default=None,
                   choices=["demo", "prod"],
                   help="API environment (default: from .env or demo).")
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
            rows.append([
                ea.event_ticker[:30],
                ea.arb_type.replace("_", " ").upper(),
                ea.n_markets,
                f"{ea.sum_ask}¢",
                f"{ea.profit_per_set}¢",
                f"{ea.roi_per_set:+.0%}",
            ])
        hdrs = ["Event", "Strategy", "Legs", "Sum Ask", "Profit/set", "ROI"]
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
    """Show live positions with current market prices and P&L."""
    cfg.validate()
    from engine.client import KalshiClient
    from engine.profit_taker import ProfitTaker
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
    for s in snapshots:
        cost_total = s.cost_basis_cents * s.quantity
        total_cost += cost_total
        total_pnl += s.pnl_if_sell
        rows.append([
            s.ticker[:35],
            s.side.upper(),
            s.quantity,
            f"{s.cost_basis_cents}¢",
            f"{s.current_bid}¢",
            f"{s.pnl_if_sell:+d}¢",
            f"{s.pnl_pct:+.0%}",
        ])

    hdrs = ["Market", "Side", "Qty", "Cost/ea", "Bid", "P&L", "P&L %"]
    print()
    if tab:
        print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    else:
        print("  ".join(hdrs))
        for r in rows:
            print("  " + "  ".join(str(c) for c in r))

    print(f"\n  Total cost: {total_cost}¢ (${total_cost/100:.2f})")
    print(f"  Total P&L if sold now: {total_pnl:+d}¢ (${total_pnl/100:+.2f})")
    print(f"  Take profit threshold: +{cfg.take_profit_cents}¢/contract")
    print(f"  Stop loss threshold:   {cfg.stop_loss_pct:.0%} of cost\n")


def cmd_run(cfg) -> None:
    """Start the live arb engine loop."""
    cfg.validate()
    from engine.trading_engine import TradingEngine
    TradingEngine(cfg).run()


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
