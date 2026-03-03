"""
estimate.py — Profit estimator + Monte Carlo simulator using live Kalshi market data.

Fetches all open markets from Kalshi's public API (no auth needed for market data),
identifies YES+NO sum arb opportunities, and either:
  --estimate:    prints expected locked profit for every signal
  --sim-returns: Monte Carlo simulates settlement of all positions

This is the ONLY strategy: buy YES + NO when sum < $1.00 after fees.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
import urllib.request
from dataclasses import dataclass
from typing import Dict, List

from engine.config import Config
from engine.scanner import FEE_PER_CONTRACT, FEE_ROUND_TRIP, MarketQuote

logger = logging.getLogger(__name__)

# Kalshi public API (no auth needed for market data)
_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_TIMEOUT = 15


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "kalshi-arb-estimator/1.0"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def fetch_all_markets(env: str = "demo") -> list[dict]:
    """Paginate through all open markets on Kalshi."""
    base = _PROD_BASE if env == "prod" else _DEMO_BASE
    all_markets: list[dict] = []
    cursor: str | None = None

    while True:
        url = f"{base}/markets?status=open&limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            data = _fetch_json(url)
        except Exception as exc:
            logger.error("Fetch failed: %s", exc)
            break
        batch = data.get("markets", [])
        if not batch:
            break
        all_markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor:
            break

    return all_markets


def parse_markets(raw: list[dict]) -> list[MarketQuote]:
    """Parse raw market dicts into MarketQuote objects."""
    markets: list[MarketQuote] = []
    for m in raw:
        ticker = m.get("ticker", "")
        if not ticker:
            continue

        yes_bid = _cents(m, "yes_bid")
        yes_ask = _cents(m, "yes_ask")
        no_bid  = _cents(m, "no_bid")
        no_ask  = _cents(m, "no_ask")

        # Derive NO from YES if not provided
        if no_bid == 0 and yes_ask > 0:
            no_bid = 100 - yes_ask
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid

        if yes_ask <= 0 or no_ask <= 0:
            continue

        markets.append(MarketQuote(
            ticker        = ticker,
            event_ticker  = m.get("event_ticker", ""),
            title         = m.get("title", m.get("subtitle", "")),
            category      = m.get("category", ""),
            status        = m.get("status", ""),
            yes_bid       = yes_bid,
            yes_ask       = yes_ask,
            no_bid        = no_bid,
            no_ask        = no_ask,
            last_price    = _cents(m, "last_price"),
            volume_24h    = int(m.get("volume_24h", 0)),
            open_interest = int(m.get("open_interest", 0)),
            close_time    = m.get("close_time", ""),
            result        = m.get("result", ""),
        ))
    return markets


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


# ── Estimate ──────────────────────────────────────────────────────────────────

@dataclass
class ArbSignal:
    ticker:      str
    title:       str
    category:    str
    yes_ask:     int    # cents
    no_ask:      int    # cents
    pair_cost:   int    # yes_ask + no_ask + fees (cents)
    profit:      int    # locked profit per pair (cents)
    quantity:    int    # max contracts given capital limit
    total_cost:  int    # pair_cost × quantity (cents)
    total_profit: int   # profit × quantity (cents)
    volume_24h:  int
    hours_left:  float

    @property
    def roi(self) -> float:
        return self.total_profit / self.total_cost if self.total_cost > 0 else 0.0


def find_arb_signals(markets: list[MarketQuote], cfg: Config) -> list[ArbSignal]:
    """Find all markets where yes_ask + no_ask < 100 - fees."""
    signals: list[ArbSignal] = []

    for mkt in markets:
        if not mkt.is_arb:
            continue
        if mkt.locked_profit_cents < cfg.min_profit_cents:
            continue

        # Calculate quantity
        pair_cost = mkt.yes_ask + mkt.no_ask + FEE_ROUND_TRIP
        qty = max(1, min(cfg.max_order_cents // pair_cost, cfg.max_contracts, 50_000))

        signals.append(ArbSignal(
            ticker=mkt.ticker,
            title=mkt.title[:60],
            category=mkt.category or "OTHER",
            yes_ask=mkt.yes_ask,
            no_ask=mkt.no_ask,
            pair_cost=pair_cost,
            profit=mkt.locked_profit_cents,
            quantity=qty,
            total_cost=pair_cost * qty,
            total_profit=mkt.locked_profit_cents * qty,
            volume_24h=mkt.volume_24h,
            hours_left=mkt.hours_to_expiry,
        ))

    signals.sort(key=lambda s: s.profit, reverse=True)
    return signals


def print_estimate(signals: list[ArbSignal], n_total: int, elapsed: float) -> None:
    try:
        from tabulate import tabulate
        tab = True
    except ImportError:
        tab = False

    total_profit = sum(s.total_profit for s in signals)
    total_cost   = sum(s.total_cost for s in signals)
    roi = total_profit / total_cost if total_cost > 0 else 0.0

    print("\n" + "=" * 76)
    print("  KALSHI ARB ESTIMATE  —  YES+NO Sum Arbitrage")
    print(f"  {n_total} markets scanned  |  {len(signals)} arb signals  |  fetched in {elapsed:.1f}s")
    print("=" * 76)
    print(f"\n  TOTAL LOCKED PROFIT   : {total_profit}¢  (${total_profit/100:,.2f})")
    print(f"  Capital required      : {total_cost}¢  (${total_cost/100:,.2f})")
    print(f"  Guaranteed ROI        : {roi:+.2%}")
    print(f"  Arb signals           : {len(signals)}")

    if not signals:
        print("\n  No arb opportunities found. YES+NO sums are within normal range.")
        print("  This is expected — Kalshi markets are tightly priced most of the time.")
        print("  The engine scans continuously to catch transient mispricings.\n")
        return

    print(f"\n" + "-" * 76)
    print("  ARB SIGNALS (sorted by profit per pair)")
    print("-" * 76)

    rows = []
    for s in signals:
        rows.append([
            s.ticker[:35],
            f"{s.yes_ask}¢",
            f"{s.no_ask}¢",
            f"{s.yes_ask + s.no_ask}¢",
            f"{s.profit}¢",
            s.quantity,
            f"${s.total_cost/100:.2f}",
            f"${s.total_profit/100:.2f}",
            f"{s.roi:+.1%}",
            s.volume_24h,
        ])

    hdrs = ["Ticker", "YES", "NO", "Sum", "Profit", "Qty", "Cost", "Total$", "ROI", "Vol24h"]
    if tab:
        print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    else:
        print("  " + "  ".join(hdrs))
        for r in rows:
            print("  " + "  ".join(str(c) for c in r))

    # By category
    cat_groups: Dict[str, list] = {}
    for s in signals:
        cat_groups.setdefault(s.category or "OTHER", []).append(s)

    if len(cat_groups) > 1:
        print(f"\n" + "-" * 76)
        print("  BY CATEGORY")
        print("-" * 76)
        rows = []
        for cat, sigs in sorted(cat_groups.items(), key=lambda x: -sum(s.total_profit for s in x[1])):
            tp = sum(s.total_profit for s in sigs)
            tc = sum(s.total_cost for s in sigs)
            rows.append([cat, len(sigs), f"${tc/100:.2f}", f"${tp/100:.2f}",
                         f"{tp/tc:+.2%}" if tc else "n/a"])
        hdrs2 = ["Category", "Signals", "Cost", "Profit", "ROI"]
        if tab:
            print(tabulate(rows, headers=hdrs2, tablefmt="rounded_outline"))
        else:
            for r in rows:
                print("  " + "  ".join(str(c) for c in r))

    print("\n" + "-" * 76)
    print("  All profits are LOCKED at time of entry — guaranteed by YES+NO = $1.00 settlement.")
    print("  Only risk: VOID resolution (event cancelled) → fees lost, principal refunded.")
    print("-" * 76 + "\n")


# ── Monte Carlo ───────────────────────────────────────────────────────────────

def simulate_returns(
    signals: list[ArbSignal],
    n_total: int,
    elapsed: float,
    n_trials: int = 1_000,
    seed: int | None = None,
    void_prob: float = 0.05,
) -> None:
    """
    Monte Carlo simulate settlement of all arb positions.

    Each trial:
      - Each position resolves normally (YES or NO wins) with prob 1-void_prob
        → collect locked profit
      - Position resolves VOID with prob void_prob
        → lose fees only (principal refunded)
    """
    import random

    if not signals:
        print("\n  No arb signals to simulate. Run --estimate first to check availability.\n")
        return

    rng = random.Random(seed)
    total_cost = sum(s.total_cost for s in signals)

    # Pre-compute per-signal outcomes
    sig_params: list[tuple[int, int]] = []  # (profit_normal, loss_void)
    for s in signals:
        profit_normal = s.total_profit                     # locked profit (cents)
        loss_void     = -(FEE_ROUND_TRIP * s.quantity)     # lose fees only (cents)
        sig_params.append((profit_normal, loss_void))

    # Run trials
    trial_pnl: list[int] = []
    for _ in range(n_trials):
        pnl = 0
        for profit_n, loss_v in sig_params:
            if rng.random() < void_prob:
                pnl += loss_v
            else:
                pnl += profit_n
        trial_pnl.append(pnl)

    trial_pnl.sort()
    n = len(trial_pnl)

    mean_pnl = statistics.mean(trial_pnl)
    std_pnl  = statistics.stdev(trial_pnl) if n > 1 else 0
    median   = statistics.median(trial_pnl)
    p5       = trial_pnl[max(0, int(n * 0.05))]
    p25      = trial_pnl[max(0, int(n * 0.25))]
    p75      = trial_pnl[min(n-1, int(n * 0.75))]
    p95      = trial_pnl[min(n-1, int(n * 0.95))]
    worst    = trial_pnl[0]
    best     = trial_pnl[-1]
    pct_pos  = sum(1 for p in trial_pnl if p > 0) / n

    # Histogram
    BINS = 16
    lo, hi = worst, best
    width = (hi - lo) / BINS if hi > lo else 1
    counts = [0] * BINS
    for p in trial_pnl:
        b = min(BINS - 1, int((p - lo) / width))
        counts[b] += 1
    bar_max = max(counts) or 1
    BAR_W = 30

    print("\n" + "=" * 76)
    print("  SIMULATED RETURNS  —  Monte Carlo / Kalshi Markets")
    print(f"  {n_total} markets  |  {len(signals)} arb positions  |  "
          f"{n_trials:,} trials  |  void_prob={void_prob:.1%}")
    print(f"  Total capital  : {total_cost}¢ (${total_cost/100:,.2f})")
    print("=" * 76)

    print(f"\n  Mean P&L              : {mean_pnl:+,.0f}¢  (${mean_pnl/100:+,.2f})  ± {std_pnl:,.0f}¢")
    print(f"  Median                : {median:+,.0f}¢  (${median/100:+,.2f})")
    print(f"  Best trial            : {best:+,.0f}¢  (${best/100:+,.2f})")
    print(f"  Worst trial           : {worst:+,.0f}¢  (${worst/100:+,.2f})")
    print(f"\n  P5  (bad)             : {p5:+,.0f}¢  (${p5/100:+,.2f})")
    print(f"  P25                   : {p25:+,.0f}¢  (${p25/100:+,.2f})")
    print(f"  P75                   : {p75:+,.0f}¢  (${p75/100:+,.2f})")
    print(f"  P95 (good)            : {p95:+,.0f}¢  (${p95/100:+,.2f})")
    print(f"\n  Probability of profit : {pct_pos:.1%}")
    if total_cost > 0:
        print(f"  Expected ROI          : {mean_pnl/total_cost:+.2%}")

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
        print(f"  {bar_lo/100:+8.2f} │{bar:<{BAR_W}}│ {counts[i]:>4}{marker}")

    print(f"\n" + "-" * 76)
    print("  Normal resolution: one side pays $1.00 → locked profit collected.")
    print(f"  VOID resolution ({void_prob:.0%} assumed): fees lost, principal refunded.")
    print("-" * 76 + "\n")


# ── Entry points ──────────────────────────────────────────────────────────────

def run_estimate(cfg: Config) -> None:
    print("\n  Fetching live Kalshi markets …", end="", flush=True)
    t0 = time.time()
    raw = fetch_all_markets(cfg.env)
    elapsed = time.time() - t0
    print(f"\r  Fetched {len(raw)} markets in {elapsed:.1f}s  —  scanning for arb …       ")

    markets = parse_markets(raw)
    signals = find_arb_signals(markets, cfg)
    print_estimate(signals, len(markets), elapsed)


def run_simulation(cfg: Config, n_trials: int = 1_000, seed: int | None = None) -> None:
    print("\n  Fetching live Kalshi markets …", end="", flush=True)
    t0 = time.time()
    raw = fetch_all_markets(cfg.env)
    elapsed = time.time() - t0
    print(f"\r  Fetched {len(raw)} markets in {elapsed:.1f}s  —  scanning for arb …       ")

    markets = parse_markets(raw)
    signals = find_arb_signals(markets, cfg)

    if signals:
        print(f"  {len(signals)} arb signals found  —  simulating {n_trials:,} settlement trials …")
    simulate_returns(signals, len(markets), elapsed, n_trials=n_trials, seed=seed)
