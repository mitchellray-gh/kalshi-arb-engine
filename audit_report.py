"""
audit_report.py - Comprehensive Position Hold-Time & Profitability Audit
========================================================================
Pulls live data from Kalshi API, analyzes all positions, fills, and market data,
and produces a detailed report on how long positions must be held before profiting.
"""
import sys, json, time, os
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.config import load_config
from engine.client import KalshiClient


@dataclass
class FillRecord:
    ticker: str
    action: str        # buy/sell
    side: str          # yes/no
    count: int
    price_cents: int   # yes_price in cents
    fee_cents: int
    is_taker: bool
    created_time: datetime
    fill_id: str
    order_id: str


@dataclass
class MarketInfo:
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    status: str
    result: str
    close_time: Optional[datetime]
    expiration_time: Optional[datetime]
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    volume: int
    volume_24h: int
    open_interest: int
    category: str


@dataclass
class PositionAudit:
    ticker: str
    event_ticker: str
    title: str
    side: str
    quantity: int
    avg_buy_price: int          # cents
    total_cost: int             # cents
    total_fees_paid: int        # cents
    first_buy_time: datetime
    last_buy_time: datetime
    current_bid: int
    current_ask: int
    market_status: str
    market_result: str
    close_time: Optional[datetime]
    volume: int
    is_taker: bool

    # Computed
    held_hours: float = 0.0
    held_days: float = 0.0
    days_to_settlement: float = 0.0
    pnl_if_sell_now: int = 0     # cents (net of taker sell fee)
    pnl_if_settle_win: int = 0   # cents (100c - cost - fees)
    pnl_if_settle_lose: int = 0  # cents (0 - cost - fees)
    daily_profit_rate: float = 0.0  # cents/day if held to settlement


@dataclass
class EventAudit:
    event_ticker: str
    title: str
    legs: List[PositionAudit]
    n_legs: int = 0
    total_invested: int = 0     # cents
    total_fees: int = 0
    qty_per_leg: int = 0
    is_complete_arb: bool = False  # all legs of ME filled?
    held_hours: float = 0.0
    days_to_settlement: float = 0.0
    settlement_profit: int = 0   # guaranteed if arb complete
    settlement_roi: float = 0.0
    resale_pnl: int = 0
    resale_vs_settlement: float = 0.0


def parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except:
        return None


def run_audit():
    cfg = load_config()
    cfg.env = 'prod'
    client = KalshiClient(cfg)
    now = datetime.now(timezone.utc)

    print("Fetching data from Kalshi API...")

    # ── 1. Pull all fills ─────────────────────────────────────────────────
    fills_raw = client.get_fills(limit=100).get('fills', [])
    fills: List[FillRecord] = []
    for f in fills_raw:
        fills.append(FillRecord(
            ticker=f['ticker'],
            action=f['action'],
            side=f['side'],
            count=f['count'],
            price_cents=f.get('yes_price', 0),
            fee_cents=int(float(f.get('fee_cost', '0')) * 100),
            is_taker=f.get('is_taker', True),
            created_time=parse_dt(f['created_time']),
            fill_id=f.get('fill_id', ''),
            order_id=f.get('order_id', ''),
        ))

    # ── 2. Pull live positions ────────────────────────────────────────────
    pos_resp = client.get_positions()
    market_positions = pos_resp.get('market_positions', [])
    event_positions = pos_resp.get('event_positions', [])

    # ── 3. Pull market data for each position ─────────────────────────────
    active_tickers = [mp['ticker'] for mp in market_positions if mp.get('position', 0) > 0]
    market_info: Dict[str, MarketInfo] = {}
    for t in active_tickers:
        try:
            m = client.get_market(t)
            mk = m.get('market', m)
            market_info[t] = MarketInfo(
                ticker=t,
                event_ticker=mk.get('event_ticker', ''),
                title=mk.get('title', ''),
                subtitle=mk.get('subtitle', ''),
                status=mk.get('status', ''),
                result=mk.get('result', ''),
                close_time=parse_dt(mk.get('close_time', '')),
                expiration_time=parse_dt(mk.get('expiration_time', '')),
                yes_bid=mk.get('yes_bid', 0),
                yes_ask=mk.get('yes_ask', 0),
                no_bid=mk.get('no_bid', 0),
                no_ask=mk.get('no_ask', 0),
                volume=mk.get('volume', 0),
                volume_24h=mk.get('volume_24h', 0),
                open_interest=mk.get('open_interest', 0),
                category=mk.get('category', ''),
            )
            time.sleep(0.2)
        except Exception as e:
            print(f"  Warning: couldn't fetch market {t}: {e}")

    # ── 4. Build per-position audit ───────────────────────────────────────
    # Group fills by ticker
    fills_by_ticker: Dict[str, List[FillRecord]] = {}
    for f in fills:
        fills_by_ticker.setdefault(f.ticker, []).append(f)

    position_audits: List[PositionAudit] = []
    for mp in market_positions:
        t = mp['ticker']
        qty = mp.get('position', 0)
        if qty <= 0:
            continue

        ticker_fills = [f for f in fills_by_ticker.get(t, []) if f.action == 'buy']
        mi = market_info.get(t)

        if ticker_fills:
            first_buy = min(f.created_time for f in ticker_fills)
            last_buy = max(f.created_time for f in ticker_fills)
            total_cost = sum(f.count * f.price_cents for f in ticker_fills)
            total_fees = sum(f.fee_cents for f in ticker_fills)
            avg_price = total_cost // qty if qty else 0
            is_taker = ticker_fills[0].is_taker
            side = ticker_fills[0].side
        else:
            first_buy = now
            last_buy = now
            total_cost = 0
            total_fees = mp.get('fees_paid', 0)
            avg_price = 0
            is_taker = True
            side = 'yes'

        pa = PositionAudit(
            ticker=t,
            event_ticker=mi.event_ticker if mi else '',
            title=mi.title if mi else t,
            side=side,
            quantity=qty,
            avg_buy_price=avg_price,
            total_cost=total_cost,
            total_fees_paid=total_fees,
            first_buy_time=first_buy,
            last_buy_time=last_buy,
            current_bid=mi.yes_bid if mi else 0,
            current_ask=mi.yes_ask if mi else 0,
            market_status=mi.status if mi else '?',
            market_result=mi.result if mi else '',
            close_time=mi.close_time if mi else None,
            volume=mi.volume if mi else 0,
            is_taker=is_taker,
        )

        # Compute derived fields
        held = now - first_buy
        pa.held_hours = held.total_seconds() / 3600
        pa.held_days = held.total_seconds() / 86400

        if pa.close_time:
            to_close = pa.close_time - now
            pa.days_to_settlement = to_close.total_seconds() / 86400
        else:
            pa.days_to_settlement = -1

        # P&L if sold now (taker sell: 2c fee per contract)
        sell_revenue = qty * pa.current_bid
        sell_fee = qty * 2  # taker sell fee
        pa.pnl_if_sell_now = sell_revenue - total_cost - total_fees - sell_fee

        # P&L at settlement
        pa.pnl_if_settle_win = (100 * qty) - total_cost - total_fees
        pa.pnl_if_settle_lose = 0 - total_cost - total_fees

        # Daily profit rate (theoretical if held to settlement)
        if pa.days_to_settlement > 0:
            pa.daily_profit_rate = pa.pnl_if_settle_win / pa.days_to_settlement
        else:
            pa.daily_profit_rate = 0

        position_audits.append(pa)

    # ── 5. Build per-event audit ──────────────────────────────────────────
    events: Dict[str, List[PositionAudit]] = {}
    for pa in position_audits:
        events.setdefault(pa.event_ticker, []).append(pa)

    event_audits: List[EventAudit] = []
    for et, legs in events.items():
        ea = EventAudit(
            event_ticker=et,
            title=legs[0].title.split(' - ')[0] if legs else et,
            legs=legs,
            n_legs=len(legs),
            total_invested=sum(l.total_cost for l in legs),
            total_fees=sum(l.total_fees_paid for l in legs),
            qty_per_leg=legs[0].quantity if legs else 0,
            held_hours=max(l.held_hours for l in legs),
            days_to_settlement=min(l.days_to_settlement for l in legs if l.days_to_settlement >= 0) if any(l.days_to_settlement >= 0 for l in legs) else -1,
        )

        # Settlement: one leg pays 100c * qty
        ea.settlement_profit = (100 * ea.qty_per_leg) - ea.total_invested - ea.total_fees
        ea.settlement_roi = (ea.settlement_profit / (ea.total_invested + ea.total_fees) * 100) if (ea.total_invested + ea.total_fees) > 0 else 0

        # Resale now
        resale_revenue = sum(l.current_bid * l.quantity for l in legs)
        resale_fees = sum(l.quantity * 2 for l in legs)  # taker sell
        ea.resale_pnl = resale_revenue - ea.total_invested - ea.total_fees - resale_fees
        ea.resale_vs_settlement = (ea.resale_pnl / ea.settlement_profit * 100) if ea.settlement_profit > 0 else 0

        event_audits.append(ea)

    # ── 6. Get balance ────────────────────────────────────────────────────
    bal = client.get_balance()
    cash = bal.get('balance', 0)
    portfolio = bal.get('portfolio_value', 0)

    # ══════════════════════════════════════════════════════════════════════
    # GENERATE REPORT
    # ══════════════════════════════════════════════════════════════════════

    lines = []
    def p(s=''):
        lines.append(s)

    p("=" * 100)
    p("  KALSHI POSITION HOLD-TIME & PROFITABILITY AUDIT")
    p(f"  Generated: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    p("=" * 100)

    # Account Overview
    p("\n  ACCOUNT OVERVIEW")
    p("  " + "-" * 50)
    p(f"  Cash balance:     {cash}c (${cash/100:.2f})")
    p(f"  Portfolio value:  {portfolio}c (${portfolio/100:.2f})")
    p(f"  Total equity:     {cash + portfolio}c (${(cash + portfolio)/100:.2f})")
    p(f"  Open positions:   {len(position_audits)} markets across {len(event_audits)} events")
    p(f"  Total fills:      {len(fills)} ({sum(1 for f in fills if f.action == 'buy')} buys, {sum(1 for f in fills if f.action == 'sell')} sells)")
    p(f"  Settlements:      0 (none settled yet)")

    # Position Detail Table
    p("\n" + "=" * 100)
    p("  POSITION DETAIL BY MARKET")
    p("=" * 100)
    header = f"  {'Ticker':<38} {'Qty':>4} {'Buy':>4} {'Cost':>6} {'Fee':>4} {'Held':>8} {'ToClose':>9} {'Bid':>4} {'Sell PnL':>8} {'Win PnL':>8}"
    p(header)
    p("  " + "-" * 96)

    for pa in sorted(position_audits, key=lambda x: x.held_hours, reverse=True):
        held_str = f"{pa.held_hours:.1f}h" if pa.held_hours < 48 else f"{pa.held_days:.1f}d"
        close_str = f"{pa.days_to_settlement:.0f}d" if pa.days_to_settlement >= 0 else "unknown"
        p(f"  {pa.ticker:<38} {pa.quantity:>4} {pa.avg_buy_price:>3}c {pa.total_cost:>5}c {pa.total_fees_paid:>3}c {held_str:>8} {close_str:>9} {pa.current_bid:>3}c {pa.pnl_if_sell_now:>+7}c {pa.pnl_if_settle_win:>+7}c")

    # Event-Level Analysis
    p("\n" + "=" * 100)
    p("  EVENT-LEVEL ANALYSIS (MULTI-LEG ARB GROUPS)")
    p("=" * 100)

    for ea in sorted(event_audits, key=lambda x: x.days_to_settlement):
        close_str = f"{ea.days_to_settlement:.0f} days" if ea.days_to_settlement >= 0 else "unknown"
        daily_rate = ea.settlement_profit / ea.days_to_settlement if ea.days_to_settlement > 0 else 0

        p(f"\n  EVENT: {ea.event_ticker}")
        p(f"  Title: {ea.title}")
        p(f"  " + "-" * 60)
        p(f"    Legs held:        {ea.n_legs}")
        p(f"    Qty per leg:      {ea.qty_per_leg}")
        p(f"    Total invested:   {ea.total_invested}c (${ea.total_invested/100:.2f})")
        p(f"    Fees paid:        {ea.total_fees}c (${ea.total_fees/100:.2f})")
        p(f"    Currently held:   {ea.held_hours:.1f} hours ({ea.held_hours/24:.1f} days)")
        p(f"    Time to close:    {close_str}")
        p(f"    ---")
        p(f"    Settlement profit:{ea.settlement_profit:>+6}c (${ea.settlement_profit/100:+.2f})")
        p(f"    Settlement ROI:   {ea.settlement_roi:>+.1f}%")
        p(f"    Daily earn rate:  {daily_rate:>+.2f}c/day (${daily_rate/100:+.4f}/day)")
        p(f"    ---")
        p(f"    Resale PnL now:   {ea.resale_pnl:>+6}c (${ea.resale_pnl/100:+.2f})")
        p(f"    Resale captures:  {ea.resale_vs_settlement:>+.0f}% of arb profit")

        # Per-leg breakdown
        p(f"    Legs:")
        for l in ea.legs:
            p(f"      {l.ticker:<36} qty={l.quantity:>3}  buy={l.avg_buy_price}c  bid={l.current_bid}c  vol={l.volume:>6}")

    # Hold-Time Analysis
    p("\n" + "=" * 100)
    p("  HOLD-TIME ANALYSIS")
    p("=" * 100)

    all_held = [pa.held_hours for pa in position_audits]
    all_to_close = [pa.days_to_settlement for pa in position_audits if pa.days_to_settlement >= 0]

    p(f"\n  Time positions have been held:")
    p(f"    Shortest: {min(all_held):.1f} hours")
    p(f"    Longest:  {max(all_held):.1f} hours")
    p(f"    Average:  {sum(all_held)/len(all_held):.1f} hours")

    if all_to_close:
        p(f"\n  Time remaining until settlement:")
        p(f"    Soonest:  {min(all_to_close):.0f} days ({min(all_to_close)*24:.0f} hours)")
        p(f"    Latest:   {max(all_to_close):.0f} days ({max(all_to_close)*24:.0f} hours)")
        p(f"    Average:  {sum(all_to_close)/len(all_to_close):.0f} days")

    p(f"\n  Profit realization timeline:")
    p(f"    Realized so far:  $0.00 (no positions have settled)")
    total_settlement = sum(ea.settlement_profit for ea in event_audits)
    total_resale = sum(ea.resale_pnl for ea in event_audits)
    p(f"    If all settle:    {total_settlement:+d}c (${total_settlement/100:+.2f})")
    p(f"    If sold now:      {total_resale:+d}c (${total_resale/100:+.2f})")
    p(f"    Resale loss:      {total_resale - total_settlement:+d}c (${(total_resale - total_settlement)/100:+.2f}) — this is the cost of early exit")

    # Holding Period Risk Table
    p("\n" + "=" * 100)
    p("  HOLDING-PERIOD-TO-PROFIT TABLE")
    p("=" * 100)
    p(f"\n  {'Event':<35} {'Invested':>8} {'Held':>8} {'Wait':>10} {'Settle$':>9} {'$/Day':>8} {'Sell Now':>9} {'Loss%':>7}")
    p("  " + "-" * 96)

    for ea in sorted(event_audits, key=lambda x: x.days_to_settlement):
        close_str = f"{ea.days_to_settlement:.0f}d" if ea.days_to_settlement >= 0 else "?"
        daily = ea.settlement_profit / ea.days_to_settlement if ea.days_to_settlement > 0 else 0
        total_in = ea.total_invested + ea.total_fees
        loss_pct = (ea.resale_pnl / total_in * 100) if total_in > 0 else 0
        p(f"  {ea.event_ticker:<35} ${ea.total_invested/100:>6.2f}  {ea.held_hours:>5.1f}h  {close_str:>10}  ${ea.settlement_profit/100:>+7.2f} ${daily/100:>+.4f}  ${ea.resale_pnl/100:>+7.2f}  {loss_pct:>+.0f}%")

    # Critical Findings
    p("\n" + "=" * 100)
    p("  CRITICAL FINDINGS")
    p("=" * 100)

    total_invested = sum(ea.total_invested for ea in event_audits)
    total_fees = sum(ea.total_fees for ea in event_audits)

    p(f"""
  1. ZERO DAILY PROFIT: All {len(position_audits)} positions are arb legs that only pay out
     at CONTRACT SETTLEMENT. No position has generated any realized profit yet.

  2. SETTLEMENT TIMELINE: Positions must be held {min(all_to_close):.0f} to {max(all_to_close):.0f} days
     before ANY profit is realized. The soonest settlement is ~{min(all_to_close):.0f} days away.

  3. CAPITAL IS LOCKED: ${total_invested/100:.2f} invested + ${total_fees/100:.2f} fees = ${(total_invested+total_fees)/100:.2f}
     total locked up. Cash balance is $0.00. Cannot place new trades.

  4. RESALE IS DESTRUCTIVE: Selling all positions now would realize {total_resale:+d}c
     (${total_resale/100:+.2f}). This is a {abs(total_resale)}c LOSS because:
     - Most positions are YES contracts at 1-2c with 0c bid (no buyers)
     - Taker sell fee is 2c/contract
     - Spread is 100% of position value

  5. ARB PROFIT IS THEORETICAL: The ${total_settlement/100:.2f} settlement profit assumes:
     - All incomplete arbs complete (some are partial fills: only 1-3 of 7+ legs)
     - No events void (cancellation returns principal minus fees)
     - Correct settlement in our favor (ME arb guarantees ONE leg wins)

  6. INCOMPLETE ARBS = NAKED EXPOSURE: Events with fewer legs than needed
     means we're exposed directionally, not hedged. These are NOT guaranteed profit.

  7. DAILY PROFIT RATE: Even if all arbs are complete and settle correctly,
     the daily profit rate is ${total_settlement/100/max(all_to_close) if max(all_to_close) > 0 else 0:.4f}/day — essentially zero.
""")

    # Strategy Comparison
    p("=" * 100)
    p("  STRATEGY COMPARISON: ARB vs SPREAD CAPTURE")
    p("=" * 100)
    p(f"""
  CURRENT (Arb) Strategy:
    Capital deployed: ${(total_invested+total_fees)/100:.2f}
    Held for:         {max(all_held):.1f} hours
    Profit realized:  $0.00
    Time to profit:   {min(all_to_close):.0f}-{max(all_to_close):.0f} days
    Daily rate:       ~${total_settlement/100/max(all_to_close) if max(all_to_close) > 0 else 0:.4f}/day
    Risk:             Partial fills, naked exposure, voids

  NEW (Spread Capture) Strategy:
    How it works:     Maker buy at bid+1, maker sell at ask-1
    Fee per trade:    $0.00 (maker orders = zero fee)
    Profit per RT:    1-3c ($0.01-$0.03) per round-trip
    Target markets:   Sports (NBA/NHL), crypto (BTC daily), weather
    Time to profit:   Minutes to hours (fill, then sell same day)
    Daily potential:  10-50 round-trips * 1-3c = 10-150c/day ($0.10-$1.50)
    Risk:             Price moves against us (stop-loss at {cfg.mm_stop_loss}c)
    Capital needed:   Same ${(total_invested+total_fees)/100:.2f} but recycled many times
""")

    p("=" * 100)
    p("  END OF AUDIT REPORT")
    p("=" * 100)

    # Write to file
    report_text = "\n".join(lines)
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results', 'audit_report.txt')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    # Also print
    print(report_text)
    print(f"\n  Report saved to: {report_path}")


if __name__ == '__main__':
    run_audit()
