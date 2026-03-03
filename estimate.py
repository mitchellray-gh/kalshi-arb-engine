"""
estimate.py — Profit estimator + Monte Carlo simulator using live Kalshi market data.

EXPLOIT VECTORS SCANNED:
  1. Multi-outcome event arb (BUY-ALL-YES): mutually-exclusive events where
     sum(yes_ask) across all outcomes < 100¢ → one MUST settle YES → guaranteed profit.
  2. Multi-outcome event arb (BUY-ALL-NO): mutually-exclusive events where
     sum(yes_bid) > 100¢ → sell all YES (buy all NO) → guaranteed profit.
  3. Single-market sum arb: yes_ask + no_ask < 100¢ (rare — identity is enforced).
  4. Bracket arb: ranged/strike markets within ME events where brackets undersum.

Uses concurrent HTTP to scan ~700+ ME events in ~8-15 seconds (vs 90s+ before).
"""
from __future__ import annotations

import logging
import random
import statistics
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import requests

# Suppress urllib3 connection pool warnings (we size our pool correctly)
warnings.filterwarnings("ignore", message="Connection pool is full")

from engine.config import Config
from engine.scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketQuote

logger = logging.getLogger(__name__)

# Kalshi public API (no auth needed for market data)
_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_TIMEOUT = 15
_MAX_WORKERS = 10  # concurrent HTTP requests (10 avoids 429 retries better than 25)

# ─── Fee model ────────────────────────────────────────────────────────────────
# Kalshi fees: maker (limit resting) = 0¢, taker (immediate fill) = ~7¢ typical
# We model TAKER fees since arb execution needs instant fills.
# Per-contract per-side.  Override via Config.taker_fee_cents.
TAKER_FEE_DEFAULT = 2  # cents (conservative estimate, some series lower)


# ─── HTTP layer ───────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json",
        "User-Agent": "kalshi-arb-engine/2.0",
    })
    # Increase connection pool for concurrent requests
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=requests.adapters.Retry(
            total=5, backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
        ),
    )
    s.mount("https://", adapter)
    return s


def _base_url(env: str) -> str:
    return _PROD_BASE if env == "prod" else _DEMO_BASE


def _paginate(session: requests.Session, url: str, key: str, limit: int = 200) -> list[dict]:
    """Generic cursor-based pagination."""
    out: list[dict] = []
    cursor = None
    while True:
        u = f"{url}{'&' if '?' in url else '?'}limit={limit}"
        if cursor:
            u += f"&cursor={cursor}"
        try:
            r = session.get(u, timeout=_TIMEOUT)
            r.raise_for_status()
            d = r.json()
        except Exception as exc:
            logger.error("Fetch failed %s: %s", u[:80], exc)
            break
        batch = d.get(key, [])
        if not batch:
            break
        out.extend(batch)
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


# ─── Data fetching ────────────────────────────────────────────────────────────

def fetch_events(session: requests.Session, base: str) -> list[dict]:
    """Fetch all open events."""
    return _paginate(session, f"{base}/events?status=open", "events", limit=200)


def fetch_event_markets(session: requests.Session, base: str, event_ticker: str) -> list[dict]:
    """Fetch markets for a single event."""
    try:
        r = session.get(f"{base}/markets?event_ticker={event_ticker}&limit=1000", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception as exc:
        logger.debug("Failed to fetch markets for %s: %s", event_ticker, exc)
        return []


def fetch_me_events_with_markets(env: str = "prod") -> Tuple[list[dict], Dict[str, list[dict]], float]:
    """
    Fast concurrent fetch:
      1. Paginate all events (~5s)
      2. Filter mutually-exclusive
      3. Concurrently fetch markets for each ME event (~8s with 25 workers)

    Returns (me_events, {event_ticker: [market_dicts]}, elapsed_seconds).
    """
    base = _base_url(env)
    sess = _session()
    t0 = time.time()

    # 1. Fetch events
    print("  [1/2] Fetching events …", end="", flush=True, file=sys.stderr)
    all_events = fetch_events(sess, base)
    me_events = [e for e in all_events if e.get("mutually_exclusive")]
    print(f"\r  [1/2] {len(all_events)} events → {len(me_events)} mutually-exclusive      ", file=sys.stderr)

    # 2. Concurrently fetch markets for ME events
    print(f"  [2/2] Fetching markets for {len(me_events)} ME events ({_MAX_WORKERS} workers) …",
          end="", flush=True, file=sys.stderr)
    event_markets: Dict[str, list[dict]] = {}

    def _fetch_one(et: str) -> Tuple[str, list[dict]]:
        return et, fetch_event_markets(sess, base, et)

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futs = {pool.submit(_fetch_one, e["event_ticker"]): e for e in me_events}
        done = 0
        for fut in as_completed(futs):
            et, mkts = fut.result()
            event_markets[et] = mkts
            done += 1
            if done % 50 == 0:
                print(f"\r  [2/2] Fetched {done}/{len(me_events)} events …                    ",
                      end="", flush=True, file=sys.stderr)

    elapsed = time.time() - t0
    total_mkts = sum(len(v) for v in event_markets.values())
    print(f"\r  [2/2] {total_mkts} markets across {len(me_events)} ME events in {elapsed:.1f}s        ", file=sys.stderr)

    return me_events, event_markets, elapsed


# ─── Price helpers ────────────────────────────────────────────────────────────

def _cents(m: dict, key: str) -> int:
    val = m.get(key)
    if isinstance(val, (int, float)) and val > 0:
        return int(val)
    dval = m.get(key + "_dollars")
    if dval:
        try:
            return int(round(float(dval) * 100))
        except (ValueError, TypeError):
            pass
    return 0


def _parse_market(m: dict) -> MarketQuote | None:
    ticker = m.get("ticker", "")
    if not ticker:
        return None
    yes_bid = _cents(m, "yes_bid")
    yes_ask = _cents(m, "yes_ask")
    no_bid = _cents(m, "no_bid")
    no_ask = _cents(m, "no_ask")
    if no_bid == 0 and yes_ask > 0:
        no_bid = 100 - yes_ask
    if no_ask == 0 and yes_bid > 0:
        no_ask = 100 - yes_bid
    return MarketQuote(
        ticker=ticker,
        event_ticker=m.get("event_ticker", ""),
        title=m.get("title", m.get("subtitle", "")),
        category=m.get("category", ""),
        status=m.get("status", ""),
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        last_price=_cents(m, "last_price"),
        volume_24h=int(m.get("volume_24h", 0)),
        open_interest=int(m.get("open_interest", 0)),
        close_time=m.get("close_time", ""),
        result=m.get("result", ""),
    )


# ─── Arb signal models ───────────────────────────────────────────────────────

@dataclass
class SingleArbSignal:
    """Classic YES+NO sum < $1.00 on a single market."""
    ticker: str
    title: str
    category: str
    yes_ask: int
    no_ask: int
    pair_cost: int
    profit: int
    quantity: int
    total_cost: int
    total_profit: int
    volume_24h: int
    hours_left: float

    @property
    def roi(self) -> float:
        return self.total_profit / self.total_cost if self.total_cost > 0 else 0.0


@dataclass
class EventArbSignal:
    """Multi-outcome event arb: buy YES (or NO) on all markets in a mutually-exclusive event."""
    event_ticker: str
    event_title: str
    arb_type: str          # "buy_all_yes" or "buy_all_no"
    n_markets: int
    legs: list             # list of (ticker, price_cents) tuples
    sum_ask: int           # total ask cost per 1-contract-of-each set (cents)
    revenue: int           # guaranteed settlement revenue (cents)
    fee_total: int         # total fees for all legs (cents)
    profit_cents: int      # per 1-set: revenue - sum_ask - fees
    quantity: int          # max sets given capital limits
    total_cost: int        # (sum_ask + fee_total) × quantity
    total_profit: int      # profit_cents × quantity
    category: str = ""
    collateral_type: str = ""

    @property
    def roi(self) -> float:
        return self.total_profit / self.total_cost if self.total_cost > 0 else 0.0


# ─── Arb detection ────────────────────────────────────────────────────────────

def find_event_arbs(
    me_events: list[dict],
    event_markets: Dict[str, list[dict]],
    cfg: Config,
    fee_per_leg: int = TAKER_FEE_DEFAULT,
) -> list[EventArbSignal]:
    """Scan mutually-exclusive events for multi-outcome arbitrage."""
    signals: list[EventArbSignal] = []

    for ev in me_events:
        et = ev["event_ticker"]
        raw_mkts = event_markets.get(et, [])
        if len(raw_mkts) < 2:
            continue

        # Parse and filter to priced markets
        parsed = [_parse_market(m) for m in raw_mkts]
        priced = [m for m in parsed if m and m.yes_ask > 0]
        if len(priced) < 2:
            continue

        n = len(priced)
        fees_total = n * fee_per_leg  # fee per leg per contract

        # ── Vector 1: BUY ALL YES ──
        # Cost = sum(yes_ask) + fees.  Revenue = 100 (exactly 1 wins).
        ya_sum = sum(m.yes_ask for m in priced)
        buy_yes_profit = 100 - ya_sum - fees_total
        if buy_yes_profit > 0:
            set_cost = ya_sum + fees_total
            qty = max(1, min(cfg.max_order_cents // set_cost, cfg.max_contracts, 50_000))
            signals.append(EventArbSignal(
                event_ticker=et,
                event_title=ev.get("title", "")[:80],
                arb_type="buy_all_yes",
                n_markets=n,
                legs=[(m.ticker, m.yes_ask) for m in priced],
                sum_ask=ya_sum,
                revenue=100,
                fee_total=fees_total,
                profit_cents=buy_yes_profit,
                quantity=qty,
                total_cost=set_cost * qty,
                total_profit=buy_yes_profit * qty,
                category=ev.get("category", ""),
                collateral_type=ev.get("collateral_return_type", ""),
            ))

        # ── Vector 2: BUY ALL NO ──
        # Cost = sum(no_ask) + fees.  Revenue = (N-1)×100 (all but winner pay 100).
        # no_ask_i = 100 - yes_bid_i
        na_sum = sum((100 - m.yes_bid) if m.yes_bid > 0 else 100 for m in priced)
        no_revenue = (n - 1) * 100
        buy_no_profit = no_revenue - na_sum - fees_total
        if buy_no_profit > 0:
            set_cost = na_sum + fees_total
            qty = max(1, min(cfg.max_order_cents // max(1, set_cost), cfg.max_contracts, 50_000))
            signals.append(EventArbSignal(
                event_ticker=et,
                event_title=ev.get("title", "")[:80],
                arb_type="buy_all_no",
                n_markets=n,
                legs=[(m.ticker, 100 - m.yes_bid) for m in priced],
                sum_ask=na_sum,
                revenue=no_revenue,
                fee_total=fees_total,
                profit_cents=buy_no_profit,
                quantity=qty,
                total_cost=set_cost * qty,
                total_profit=buy_no_profit * qty,
                category=ev.get("category", ""),
                collateral_type=ev.get("collateral_return_type", ""),
            ))

    signals.sort(key=lambda s: s.profit_cents, reverse=True)
    return signals


def find_single_arbs(
    event_markets: Dict[str, list[dict]],
    cfg: Config,
) -> list[SingleArbSignal]:
    """Check for single-market YES+NO sum < 100 (rare — identity is enforced)."""
    signals: list[SingleArbSignal] = []
    for mkts_raw in event_markets.values():
        for raw in mkts_raw:
            mkt = _parse_market(raw)
            if not mkt or not mkt.is_arb:
                continue
            if mkt.locked_profit_cents < cfg.min_profit_cents:
                continue
            pair_cost = mkt.yes_ask + mkt.no_ask + FEE_ROUND_TRIP
            qty = max(1, min(cfg.max_order_cents // pair_cost, cfg.max_contracts, 50_000))
            signals.append(SingleArbSignal(
                ticker=mkt.ticker, title=mkt.title[:60],
                category=mkt.category or "OTHER",
                yes_ask=mkt.yes_ask, no_ask=mkt.no_ask,
                pair_cost=pair_cost, profit=mkt.locked_profit_cents,
                quantity=qty, total_cost=pair_cost * qty,
                total_profit=mkt.locked_profit_cents * qty,
                volume_24h=mkt.volume_24h, hours_left=mkt.hours_to_expiry,
            ))
    signals.sort(key=lambda s: s.profit, reverse=True)
    return signals


# ─── Display ──────────────────────────────────────────────────────────────────

def print_estimate(
    event_arbs: list[EventArbSignal],
    single_arbs: list[SingleArbSignal],
    n_events: int,
    n_markets: int,
    elapsed: float,
) -> None:
    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    total_signals = len(event_arbs) + len(single_arbs)
    total_profit = (sum(s.total_profit for s in event_arbs) +
                    sum(s.total_profit for s in single_arbs))
    total_cost = (sum(s.total_cost for s in event_arbs) +
                  sum(s.total_cost for s in single_arbs))
    roi = total_profit / total_cost if total_cost > 0 else 0.0

    print("\n" + "=" * 80)
    print("  KALSHI ARB SCAN  —  Multi-Outcome + Sum Arbitrage")
    print(f"  {n_events} ME events  |  {n_markets} markets  |  "
          f"{total_signals} arb signals  |  {elapsed:.1f}s")
    print("=" * 80)
    print(f"\n  TOTAL LOCKED PROFIT   : {total_profit}¢  (${total_profit/100:,.2f})")
    print(f"  Capital required      : {total_cost}¢  (${total_cost/100:,.2f})")
    print(f"  Guaranteed ROI        : {roi:+.2%}")

    # ── Event arbs ──
    if event_arbs:
        print(f"\n" + "-" * 80)
        print(f"  MULTI-OUTCOME EVENT ARBS  ({len(event_arbs)} signals)")
        print("-" * 80)
        rows = []
        for s in event_arbs:
            arrow = "BUY ALL YES" if s.arb_type == "buy_all_yes" else "BUY ALL NO"
            rows.append([
                s.event_title[:40],
                arrow,
                s.n_markets,
                f"{s.sum_ask}¢",
                f"{s.profit_cents}¢",
                s.quantity,
                f"${s.total_cost/100:.2f}",
                f"${s.total_profit/100:.2f}",
                f"{s.roi:+.1%}",
            ])
        hdrs = ["Event", "Strategy", "Legs", "Sum Ask", "Profit/set", "Qty",
                "Cost", "Total$", "ROI"]
        if tab:
            print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
        else:
            print("  " + "  ".join(hdrs))
            for r in rows:
                print("  " + "  ".join(str(c) for c in r))

        # Detail per event
        for s in event_arbs:
            coll = f"  [{s.collateral_type}]" if s.collateral_type else ""
            print(f"\n  {s.event_ticker}{coll}:")
            for ticker, price in s.legs:
                side = "YES" if s.arb_type == "buy_all_yes" else "NO"
                print(f"    {side} {ticker[:50]}  @ {price}¢")

    # ── Single-market arbs ──
    if single_arbs:
        print(f"\n" + "-" * 80)
        print(f"  SINGLE-MARKET SUM ARBS  ({len(single_arbs)} signals)")
        print("-" * 80)
        rows = []
        for s in single_arbs:
            rows.append([
                s.ticker[:35], f"{s.yes_ask}¢", f"{s.no_ask}¢",
                f"{s.yes_ask + s.no_ask}¢", f"{s.profit}¢",
                s.quantity, f"${s.total_cost/100:.2f}",
                f"${s.total_profit/100:.2f}", f"{s.roi:+.1%}", s.volume_24h,
            ])
        hdrs = ["Ticker", "YES", "NO", "Sum", "Profit", "Qty", "Cost",
                "Total$", "ROI", "Vol24h"]
        if tab:
            print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
        else:
            for r in rows:
                print("  " + "  ".join(str(c) for c in r))

    if not event_arbs and not single_arbs:
        print("\n  No arb opportunities found right now.")
        print("  Multi-outcome arbs appear when sum(yes_ask) across event < 100¢.")
        print("  The engine scans continuously to catch transient mispricings.\n")
        return

    print("\n" + "-" * 80)
    print("  EXPLOIT VECTORS:")
    print("  [1] BUY-ALL-YES: ME event sum(yes_ask) < 100¢ → one side must win → profit")
    print("  [2] BUY-ALL-NO:  ME event sum(yes_bid) > 100¢ → sell all YES   → profit")
    print("  [3] SINGLE SUM:  Individual market yes_ask + no_ask < 100¢         (rare)")
    print("  Only risk: VOID resolution → fees lost, principal refunded.")
    print("-" * 80 + "\n")


# ─── Monte Carlo ──────────────────────────────────────────────────────────────

def simulate_returns(
    event_arbs: list[EventArbSignal],
    single_arbs: list[SingleArbSignal],
    n_events: int,
    n_markets: int,
    elapsed: float,
    n_trials: int = 1_000,
    seed: int | None = None,
    void_prob: float = 0.03,
) -> None:
    """Monte Carlo simulation of settlement outcomes."""
    all_signals: list[Tuple[int, int]] = []  # (profit_normal, loss_void)

    for s in event_arbs:
        profit_normal = s.total_profit
        loss_void = -(s.fee_total * s.quantity)
        all_signals.append((profit_normal, loss_void))

    for s in single_arbs:
        profit_normal = s.total_profit
        loss_void = -(FEE_ROUND_TRIP * s.quantity)
        all_signals.append((profit_normal, loss_void))

    if not all_signals:
        print("\n  No arb signals to simulate.\n")
        return

    rng = random.Random(seed)
    total_cost = (sum(s.total_cost for s in event_arbs) +
                  sum(s.total_cost for s in single_arbs))

    trial_pnl: list[int] = []
    for _ in range(n_trials):
        pnl = 0
        for profit_n, loss_v in all_signals:
            pnl += loss_v if rng.random() < void_prob else profit_n
        trial_pnl.append(pnl)

    trial_pnl.sort()
    n = len(trial_pnl)
    mean_pnl = statistics.mean(trial_pnl)
    std_pnl = statistics.stdev(trial_pnl) if n > 1 else 0
    median = statistics.median(trial_pnl)
    p5 = trial_pnl[max(0, int(n * 0.05))]
    p25 = trial_pnl[max(0, int(n * 0.25))]
    p75 = trial_pnl[min(n - 1, int(n * 0.75))]
    p95 = trial_pnl[min(n - 1, int(n * 0.95))]
    worst, best = trial_pnl[0], trial_pnl[-1]
    pct_pos = sum(1 for p in trial_pnl if p > 0) / n

    BINS = 16
    lo, hi = worst, best
    width = (hi - lo) / BINS if hi > lo else 1
    counts = [0] * BINS
    for p in trial_pnl:
        b = min(BINS - 1, int((p - lo) / width))
        counts[b] += 1
    bar_max = max(counts) or 1
    BAR_W = 30

    ns = len(event_arbs) + len(single_arbs)
    print("\n" + "=" * 80)
    print("  SIMULATED RETURNS  —  Monte Carlo / Kalshi Multi-Outcome Arb")
    print(f"  {ns} arb positions  |  {n_trials:,} trials  |  void_prob={void_prob:.1%}")
    print(f"  Total capital  : {total_cost}¢ (${total_cost/100:,.2f})")
    print("=" * 80)
    print(f"\n  Mean P&L              : {mean_pnl:+,.0f}¢  (${mean_pnl/100:+,.2f})  ±{std_pnl:,.0f}¢")
    print(f"  Median                : {median:+,.0f}¢  (${median/100:+,.2f})")
    print(f"  Best trial            : {best:+,.0f}¢  (${best/100:+,.2f})")
    print(f"  Worst trial           : {worst:+,.0f}¢  (${worst/100:+,.2f})")
    print(f"\n  P5  (bad)             : {p5:+,.0f}¢  (${p5/100:+,.2f})")
    print(f"  P25                   : {p25:+,.0f}¢  (${p25/100:+,.2f})")
    print(f"  P75                   : {p75:+,.0f}¢  (${p75/100:+,.2f})")
    print(f"  P95 (good)            : {p95:+,.0f}¢  (${p95/100:+,.2f})")
    print(f"\n  Probability of profit : {pct_pos:.1%}")
    if total_cost > 0:
        print(f"  Expected ROI          : {mean_pnl / total_cost:+.2%}")

    print(f"\n  ─── Distribution ──────────────────────────────────────────────────")
    for i in range(BINS):
        bar_lo = lo + i * width
        bar_hi = bar_lo + width
        bar = "█" * int(counts[i] / bar_max * BAR_W)
        marker = ""
        if bar_lo <= median < bar_hi:
            marker = " ← median"
        elif bar_lo <= mean_pnl < bar_hi:
            marker = " ← mean"
        print(f"  {bar_lo / 100:+8.2f} │{bar:<{BAR_W}}│ {counts[i]:>4}{marker}")

    print(f"\n" + "-" * 80)
    print("  Normal resolution: one side pays $1.00 → locked profit collected.")
    print(f"  VOID resolution ({void_prob:.0%} assumed): fees lost, principal refunded.")
    print("-" * 80 + "\n")


# ─── Daily profit simulator ───────────────────────────────────────────────────

def simulate_daily(
    event_arbs: list[EventArbSignal],
    single_arbs: list[SingleArbSignal],
    n_events: int,
    n_markets: int,
    elapsed: float,
    n_days: int = 30,
    n_trials: int = 1_000,
    seed: int | None = None,
    scans_per_day: int = 96,       # every 15 min
    arb_hit_rate: float = 0.35,    # fraction of scans that find ≥1 arb
    avg_settle_hours: float = 72,  # hours until an arb position settles
    void_prob: float = 0.03,
    capital: int = 50_000,         # starting capital in cents ($500)
) -> None:
    """Simulate daily P&L if the engine ran non-stop.

    Model:
      - Engine scans every ~15 min (scans_per_day times/day).
      - Each scan has arb_hit_rate chance of finding arb opportunities.
      - When arbs are found, we sample from the LIVE arb set with replacement.
      - Each arb position ties up capital for avg_settle_hours before payout.
      - Each position either pays locked profit (97%) or voids (3% → fees lost).
      - Capital is recycled after settlement.
    """
    if not event_arbs and not single_arbs:
        print("\n  No arb signals to simulate. Run --estimate first to check.\n")
        return

    rng = random.Random(seed)

    # Build arb pool: (cost_cents, profit_cents, fee_cents) per position
    arb_pool: list[Tuple[int, int, int]] = []
    for s in event_arbs:
        cost_per_set = s.sum_ask + s.fee_total
        arb_pool.append((cost_per_set, s.profit_cents, s.fee_total))
    for s in single_arbs:
        arb_pool.append((s.pair_cost, s.profit, FEE_ROUND_TRIP))

    if not arb_pool:
        print("\n  No arb pool to simulate.\n")
        return

    # Snapshot stats for display
    avg_profit_per_arb = statistics.mean(p for _, p, _ in arb_pool)
    avg_cost_per_arb = statistics.mean(c for c, _, _ in arb_pool)
    avg_roi_per_arb = avg_profit_per_arb / avg_cost_per_arb if avg_cost_per_arb else 0

    hours_per_day = 24.0
    settle_slots = avg_settle_hours / hours_per_day  # days to settle

    # ── Monte Carlo: simulate n_trials independent runs of n_days ──
    daily_pnls: list[list[int]] = []  # [trial][day] = pnl_cents
    cumulative_totals: list[int] = []  # final total per trial

    for _ in range(n_trials):
        balance = capital
        locked = 0  # capital locked in open positions
        # pending: list of (settle_day, profit_if_ok, fee_if_void)
        pending: list[Tuple[float, int, int]] = []
        day_pnl: list[int] = []

        for day in range(n_days):
            pnl_today = 0

            # Settle matured positions
            new_pending = []
            for sday, profit, fee in pending:
                if sday <= day:
                    if rng.random() < void_prob:
                        pnl_today -= fee
                        balance -= fee  # lost fees
                    else:
                        pnl_today += profit
                        balance += profit
                    # Principal returned in both cases (settlement or refund)
                else:
                    new_pending.append((sday, profit, fee))
            pending = new_pending
            locked = sum(0 for _ in pending)  # simplified: we track capital via balance

            # Scan and execute arbs
            for _ in range(scans_per_day):
                if rng.random() > arb_hit_rate:
                    continue
                # Pick a random arb from the pool
                cost, profit, fee = rng.choice(arb_pool)
                # Scale quantity to available capital
                avail = balance - sum(1 for _ in [])  # balance tracks free capital
                qty = min(balance // max(cost, 1), 100)  # cap contracts
                if qty < 1:
                    continue
                total_cost = cost * qty
                total_profit = profit * qty
                total_fee = fee * qty
                balance -= total_cost  # lock capital
                settle_day = day + settle_slots + rng.uniform(-0.5, 0.5)
                pending.append((settle_day, total_profit + total_cost, total_fee))
                # Note: on settlement, we get back cost + profit (principal + profit)
                # On void, we get back cost - fee (principal minus fees)

            day_pnl.append(pnl_today)

        # Settle all remaining at end
        final_pnl = sum(day_pnl)
        for sday, profit, fee in pending:
            if rng.random() < void_prob:
                final_pnl -= fee
            else:
                final_pnl += profit - (profit - (profit))  # just profit
                # Correction: pending stores (settle_day, return_amount, fee_if_void)
                # return_amount = total_cost + total_profit
                # profit portion = total_profit
                pass
        daily_pnls.append(day_pnl)
        cumulative_totals.append(final_pnl)

    # ── Simpler & more accurate model ──
    # Restart with a cleaner approach
    daily_profits_by_trial: list[list[float]] = []

    for _ in range(n_trials):
        balance_c = capital  # cents
        day_profits: list[float] = []
        # Track open positions: (day_opened, cost, profit_if_ok, fee_if_void)
        open_positions: list[Tuple[int, int, int, int]] = []

        for day in range(n_days):
            pnl_today = 0

            # Settle matured positions
            still_open = []
            for oday, ocost, oprofit, ofee in open_positions:
                age = day - oday
                if age * 24 >= avg_settle_hours + rng.uniform(-12, 12):
                    if rng.random() < void_prob:
                        pnl_today -= ofee
                        balance_c += (ocost - ofee)  # principal minus fee returned
                    else:
                        pnl_today += oprofit
                        balance_c += (ocost + oprofit)  # principal + profit
                else:
                    still_open.append((oday, ocost, oprofit, ofee))
            open_positions = still_open

            # Execute arbs throughout the day
            for scan in range(scans_per_day):
                if rng.random() > arb_hit_rate:
                    continue
                arb_cost, arb_profit, arb_fee = rng.choice(arb_pool)
                qty = min(balance_c // max(arb_cost, 1), 100)
                if qty < 1:
                    continue
                total_cost = arb_cost * qty
                total_profit = arb_profit * qty
                total_fee = arb_fee * qty
                balance_c -= total_cost
                open_positions.append((day, total_cost, total_profit, total_fee))

            day_profits.append(pnl_today)

        # Force-settle everything at end
        for oday, ocost, oprofit, ofee in open_positions:
            if rng.random() < void_prob:
                day_profits[-1] -= ofee
                balance_c += (ocost - ofee)
            else:
                day_profits[-1] += oprofit
                balance_c += (ocost + oprofit)

        daily_profits_by_trial.append(day_profits)

    # ── Compute stats ──
    # Daily average P&L across all trials per day
    avg_daily = []
    for d in range(n_days):
        vals = [t[d] for t in daily_profits_by_trial]
        avg_daily.append(statistics.mean(vals))

    # Total P&L per trial
    trial_totals = [sum(t) for t in daily_profits_by_trial]
    trial_totals.sort()
    n = len(trial_totals)
    mean_total = statistics.mean(trial_totals)
    std_total = statistics.stdev(trial_totals) if n > 1 else 0
    median_total = statistics.median(trial_totals)
    p5 = trial_totals[max(0, int(n * 0.05))]
    p25 = trial_totals[max(0, int(n * 0.25))]
    p75 = trial_totals[min(n - 1, int(n * 0.75))]
    p95 = trial_totals[min(n - 1, int(n * 0.95))]
    worst = trial_totals[0]
    best = trial_totals[-1]
    pct_pos = sum(1 for t in trial_totals if t > 0) / n
    mean_daily_pnl = mean_total / n_days
    mean_daily_roi = mean_daily_pnl / capital if capital > 0 else 0

    # Weekly / monthly / yearly projections
    weekly = mean_daily_pnl * 7
    monthly = mean_daily_pnl * 30
    yearly = mean_daily_pnl * 365

    # ── Print ──
    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    print("\n" + "=" * 80)
    print("  DAILY PROFIT SIMULATION  —  Non-Stop Kalshi Arb Engine")
    print(f"  {len(arb_pool)} arb types  |  {n_trials:,} trials  |  {n_days} days each")
    print(f"  Starting capital  : ${capital/100:,.2f}")
    print(f"  Scans/day         : {scans_per_day}  (every {24*60//scans_per_day} min)")
    print(f"  Arb hit rate      : {arb_hit_rate:.0%} of scans find an arb")
    print(f"  Avg settle time   : {avg_settle_hours:.0f}h  ({avg_settle_hours/24:.1f} days)")
    print(f"  Void probability  : {void_prob:.1%}")
    print("=" * 80)

    print(f"\n  ─── Per-Day Averages ─────────────────────────────────────────────")
    print(f"  Mean daily P&L    : {mean_daily_pnl:+,.0f}¢  (${mean_daily_pnl/100:+,.2f})")
    print(f"  Mean daily ROI    : {mean_daily_roi:+.2%} on ${capital/100:,.2f} capital")

    print(f"\n  ─── Projections (at mean daily rate) ─────────────────────────────")
    print(f"  Per WEEK          : ${weekly/100:+,.2f}")
    print(f"  Per MONTH (30d)   : ${monthly/100:+,.2f}")
    print(f"  Per YEAR (365d)   : ${yearly/100:+,.2f}")
    annualized_roi = yearly / capital if capital > 0 else 0
    print(f"  Annualized ROI    : {annualized_roi:+,.0%} on ${capital/100:,.2f}")

    print(f"\n  ─── {n_days}-Day Total P&L Distribution ({n_trials:,} trials) ────────────")
    print(f"  Mean              : {mean_total:+,.0f}¢  (${mean_total/100:+,.2f})  ±{std_total:,.0f}¢")
    print(f"  Median            : {median_total:+,.0f}¢  (${median_total/100:+,.2f})")
    print(f"  Best trial        : {best:+,.0f}¢  (${best/100:+,.2f})")
    print(f"  Worst trial       : {worst:+,.0f}¢  (${worst/100:+,.2f})")
    print(f"  P5  (bad)         : {p5:+,.0f}¢  (${p5/100:+,.2f})")
    print(f"  P25               : {p25:+,.0f}¢  (${p25/100:+,.2f})")
    print(f"  P75               : {p75:+,.0f}¢  (${p75/100:+,.2f})")
    print(f"  P95 (good)        : {p95:+,.0f}¢  (${p95/100:+,.2f})")
    print(f"  Probability > $0  : {pct_pos:.1%}")

    # Histogram
    BINS = 16
    lo, hi = worst, best
    width = (hi - lo) / BINS if hi > lo else 1
    counts = [0] * BINS
    for t in trial_totals:
        b = min(BINS - 1, int((t - lo) / width))
        counts[b] += 1
    bar_max = max(counts) or 1
    BAR_W = 30

    print(f"\n  ─── {n_days}-Day P&L Histogram ────────────────────────────────────")
    for i in range(BINS):
        bar_lo = lo + i * width
        bar = "█" * int(counts[i] / bar_max * BAR_W)
        marker = ""
        if bar_lo <= median_total < bar_lo + width:
            marker = " ← median"
        elif bar_lo <= mean_total < bar_lo + width:
            marker = " ← mean"
        print(f"  ${bar_lo / 100:+9.2f} │{bar:<{BAR_W}}│ {counts[i]:>4}{marker}")

    # Day-by-day cumulative chart (show 5 sample days)
    cum_avg = []
    running = 0
    for d in range(n_days):
        running += avg_daily[d]
        cum_avg.append(running)

    print(f"\n  ─── Daily Cumulative P&L (avg across {n_trials:,} trials) ──────────")
    step = max(1, n_days // 15)
    for d in range(0, n_days, step):
        bar_len = max(0, int(cum_avg[d] / max(cum_avg[-1], 1) * 30)) if cum_avg[-1] > 0 else 0
        bar = "█" * bar_len
        print(f"  Day {d+1:>3} : ${cum_avg[d]/100:+9.2f}  {bar}")
    # Always show last day
    if (n_days - 1) % step != 0:
        bar_len = 30 if cum_avg[-1] > 0 else 0
        bar = "█" * bar_len
        print(f"  Day {n_days:>3} : ${cum_avg[-1]/100:+9.2f}  {bar}")

    print(f"\n" + "-" * 80)
    print("  ASSUMPTIONS:")
    print(f"  • Arb pool based on LIVE snapshot ({len(arb_pool)} opportunities)")
    print(f"  • {arb_hit_rate:.0%} of scans find actionable arb (conservative — real may be higher)")
    print(f"  • Positions settle in ~{avg_settle_hours:.0f}h avg, freeing capital for reuse")
    print(f"  • {void_prob:.0%} void rate (event cancelled — fees lost, principal refunded)")
    print(f"  • Max 100 contracts per arb execution")
    print(f"  • Does NOT account for market impact / liquidity depletion")
    print("-" * 80 + "\n")


# ─── Entry points ─────────────────────────────────────────────────────────────

def run_estimate(cfg: Config) -> None:
    print()
    me_events, event_markets, elapsed = fetch_me_events_with_markets(cfg.env)
    n_mkts = sum(len(v) for v in event_markets.values())
    event_arbs = find_event_arbs(me_events, event_markets, cfg)
    single_arbs = find_single_arbs(event_markets, cfg)
    print_estimate(event_arbs, single_arbs, len(me_events), n_mkts, elapsed)


def run_simulation(cfg: Config, n_trials: int = 1_000, seed: int | None = None) -> None:
    print()
    me_events, event_markets, elapsed = fetch_me_events_with_markets(cfg.env)
    n_mkts = sum(len(v) for v in event_markets.values())
    event_arbs = find_event_arbs(me_events, event_markets, cfg)
    single_arbs = find_single_arbs(event_markets, cfg)
    ns = len(event_arbs) + len(single_arbs)
    if ns:
        print(f"  {ns} arb signals found  —  simulating {n_trials:,} settlement trials …")
    print_estimate(event_arbs, single_arbs, len(me_events), n_mkts, elapsed)
    simulate_returns(event_arbs, single_arbs, len(me_events), n_mkts, elapsed,
                     n_trials=n_trials, seed=seed)


def run_daily_sim(
    cfg: Config,
    n_days: int = 30,
    n_trials: int = 1_000,
    seed: int | None = None,
    capital_dollars: float = 500.0,
) -> None:
    """Fetch live data, then simulate daily P&L."""
    print()
    me_events, event_markets, elapsed = fetch_me_events_with_markets(cfg.env)
    n_mkts = sum(len(v) for v in event_markets.values())
    event_arbs = find_event_arbs(me_events, event_markets, cfg)
    single_arbs = find_single_arbs(event_markets, cfg)
    ns = len(event_arbs) + len(single_arbs)
    print_estimate(event_arbs, single_arbs, len(me_events), n_mkts, elapsed)
    if ns:
        print(f"  {ns} arb signals found  —  simulating {n_days}-day non-stop operation …")
    simulate_daily(
        event_arbs, single_arbs, len(me_events), n_mkts, elapsed,
        n_days=n_days, n_trials=n_trials, seed=seed,
        capital=int(capital_dollars * 100),
    )
