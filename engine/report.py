"""
engine/report.py — P&L report for arb positions.
"""
from __future__ import annotations

from typing import List

from .positions import ArbPosition


def print_report(positions: List[ArbPosition]) -> None:
    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    if not positions:
        print("\n  No positions recorded yet.\n")
        return

    open_pos = [p for p in positions if p.status == "open"]
    settled  = [p for p in positions if p.status != "open"]

    total_deployed = sum(p.total_cost for p in positions) / 100
    total_locked   = sum(p.total_profit for p in positions) / 100
    total_actual   = sum(p.actual_pnl for p in settled) / 100
    open_locked    = sum(p.total_profit for p in open_pos) / 100

    print("\n" + "=" * 72)
    print("  KALSHI ARB P&L REPORT")
    print("=" * 72)
    print(f"\n  Total positions     : {len(positions)}")
    print(f"  Open                : {len(open_pos)}")
    print(f"  Settled             : {len(settled)}")
    print(f"  Capital deployed    : ${total_deployed:,.2f}")
    print(f"  Total locked profit : ${total_locked:,.2f}")
    if settled:
        print(f"  Realised P&L        : ${total_actual:,.2f}")
    if open_pos:
        print(f"  Open locked profit  : ${open_locked:,.2f}")
    if total_deployed > 0:
        print(f"  ROI (locked)        : {total_locked/total_deployed:+.2%}")

    print("\n" + "-" * 72)
    print("  POSITIONS")
    print("-" * 72)

    rows = []
    for p in positions:
        rows.append([
            p.ticker[:35],
            f"{p.yes_price}¢",
            f"{p.no_price}¢",
            f"{p.yes_price + p.no_price}¢",
            p.quantity,
            f"${p.total_cost/100:.2f}",
            f"{p.locked_profit}¢",
            f"${p.total_profit/100:.2f}",
            p.status.upper(),
            f"${p.actual_pnl/100:.2f}" if p.actual_pnl else "-",
        ])

    hdrs = ["Ticker", "YES", "NO", "Sum", "Qty", "Cost", "Lock/pr", "Locked$", "Status", "Actual"]
    if tab:
        print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    else:
        print("  " + "  ".join(hdrs))
        for r in rows:
            print("  " + "  ".join(str(c) for c in r))

    print("-" * 72 + "\n")
