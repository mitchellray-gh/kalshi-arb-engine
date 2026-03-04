"""
backtest.py — Historical backtest of the NOAA weather trading strategy.

Methodology:
  1. Fetch all settled Kalshi weather events (Mar 1-3, 2026 = 84 events)
  2. For each, get actual temperature from expiration_value
  3. Monte Carlo (500 trials per event):
     a. Simulate NOAA forecast = actual_temp + N(0, forecast_error_sigma)
     b. Generate realistic market prices using a "crowd model" (higher sigma)
     c. Run our probability model on the NOAA forecast
     d. Identify edges (model_prob - market_ask/100 > min_edge)
     e. Simulate trade: buy at market_ask, settle at 100¢ (win) or 0¢ (lose)
  4. Aggregate P&L, win rate, Sharpe ratio, drawdown, etc.

Key assumption: Our NOAA model (sigma=2.5°F) is more accurate than the market
consensus (sigma=3.5-4.5°F). This information asymmetry creates the edge.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.client import KalshiClient
from engine.config import load_config
from engine.weather_model import _norm_cdf


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class SettledMarket:
    """One bracket within a settled weather event."""
    ticker: str
    event_ticker: str
    result: str             # 'yes' or 'no'
    floor_strike: float
    cap_strike: Optional[float]
    strike_type: str        # 'greater', 'less', 'between'
    yes_sub_title: str
    volume: int
    expiration_value: float  # actual recorded temperature
    is_winner: bool


@dataclass
class SettledEvent:
    """A full settled weather event with all its brackets."""
    event_ticker: str
    city: str
    market_type: str        # 'high' or 'low'
    date: str
    actual_temp_f: float
    markets: List[SettledMarket] = field(default_factory=list)


@dataclass
class Trade:
    """A simulated trade in the backtest."""
    ticker: str
    city: str
    entry_price_cents: int
    model_prob: float
    edge: float
    won: bool
    pnl_cents: float        # +98 if won (100 - 2¢ fee), -entry_price if lost


@dataclass
class BacktestResult:
    """Aggregated backtest metrics."""
    total_events: int = 0
    total_markets_scanned: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_cents: float = 0
    total_invested_cents: float = 0
    trades_per_day: float = 0
    win_rate: float = 0
    avg_edge: float = 0
    avg_pnl_per_trade_cents: float = 0
    roi: float = 0
    sharpe_ratio: float = 0
    max_drawdown_cents: float = 0
    daily_pnls: List[float] = field(default_factory=list)
    all_trades: List[Trade] = field(default_factory=list)
    by_city: Dict[str, dict] = field(default_factory=dict)
    by_confidence: Dict[str, dict] = field(default_factory=dict)


# ── Probability Helpers ──────────────────────────────────────────────────────

def prob_between(forecast: float, lo: float, hi: float, sigma: float) -> float:
    """P(lo < T < hi) where T ~ N(forecast, sigma)."""
    return _norm_cdf((hi - forecast) / sigma) - _norm_cdf((lo - forecast) / sigma)


def prob_above(forecast: float, threshold: float, sigma: float) -> float:
    """P(T > threshold) where T ~ N(forecast, sigma)."""
    return 1.0 - _norm_cdf((threshold - forecast) / sigma)


def prob_below(forecast: float, threshold: float, sigma: float) -> float:
    """P(T <= threshold) where T ~ N(forecast, sigma)."""
    return _norm_cdf((threshold - forecast) / sigma)


def compute_bracket_probs(
    forecast: float, brackets: List[dict], sigma: float
) -> List[Tuple[dict, float]]:
    """
    Compute probability for each bracket given a forecast.
    
    Each bracket dict has: floor_strike, cap_strike, strike_type, ticker
    Returns list of (bracket_dict, probability).
    """
    results = []
    for b in brackets:
        fs = b['floor_strike']
        cs = b.get('cap_strike')
        st = b['strike_type']

        if st == 'greater':
            # "Will temp be > floor_strike?" → YES means temp > fs
            p = prob_above(forecast, fs, sigma)
        elif st == 'less':
            p = prob_below(forecast, fs, sigma)
        elif st == 'between' and cs is not None:
            p = prob_between(forecast, fs, cs, sigma)
        else:
            # For bracket markets (B-type): floor is lower bound, cap is upper
            # If no explicit type, use floor/cap
            if cs is not None:
                p = prob_between(forecast, fs, cs, sigma)
            else:
                p = prob_above(forecast, fs, sigma)

        results.append((b, max(0.001, min(0.999, p))))
    return results


def generate_market_prices(
    brackets: List[dict],
    crowd_forecast: float,
    crowd_sigma: float,
    overround: float = 1.15,
) -> Dict[str, int]:
    """
    Generate realistic market ask prices for each bracket.
    
    Uses a "crowd model" with higher sigma (less accurate than our NOAA model)
    and applies an overround factor (sum_ask > 100¢).
    
    Returns dict of ticker → ask_price_cents.
    """
    # Compute crowd probabilities
    crowd_probs = compute_bracket_probs(crowd_forecast, brackets, crowd_sigma)
    
    # Apply overround and convert to cents
    prices = {}
    for b, p in crowd_probs:
        raw_ask = p * overround * 100
        # Clamp to valid range [1, 99] and round
        ask = max(1, min(99, round(raw_ask)))
        prices[b['ticker']] = ask
    
    return prices


# ── Data Fetching ────────────────────────────────────────────────────────────

def fetch_settled_events(client: KalshiClient) -> List[SettledEvent]:
    """Fetch all settled weather events from Kalshi."""
    from engine.weather_fetcher import KALSHI_CITY_MAP

    # Get settled weather events
    events_raw = []
    cursor = None
    for _ in range(30):
        resp = client.get_events(status='settled', cursor=cursor)
        batch = resp.get('events', [])
        for e in batch:
            if e.get('category') == 'Climate and Weather':
                et = e.get('event_ticker', '')
                # Skip monthly events, only daily temp
                if 'MONTH' in et or 'RAIN' in et:
                    continue
                events_raw.append(e)
        cursor = resp.get('cursor')
        if not cursor:
            break

    print(f"  Found {len(events_raw)} settled daily weather events")

    # For each event, get markets with settlement data
    settled = []
    for i, evt in enumerate(events_raw):
        et = evt['event_ticker']
        prefix = et.split('-')[0]

        # Determine city and market type
        city = KALSHI_CITY_MAP.get(prefix, '')
        if not city:
            continue

        if 'HIGH' in prefix:
            mtype = 'high'
        elif 'LOW' in prefix:
            mtype = 'low'
        else:
            continue

        # Parse date from ticker (e.g., KXHIGHNY-26MAR03 → 2026-03-03)
        date_part = et.split('-')[-1] if '-' in et else ''

        # Fetch settled markets
        try:
            mresp = client.get_markets(event_ticker=et, status='settled')
        except Exception as e:
            print(f"  WARN: Failed to get markets for {et}: {e}")
            continue

        mkts = mresp.get('markets', [])
        if not mkts:
            continue

        # Find actual temp from expiration_value
        actual_temp = None
        for m in mkts:
            ev = m.get('expiration_value')
            if ev is not None:
                actual_temp = float(ev)
                break

        if actual_temp is None:
            continue

        se = SettledEvent(
            event_ticker=et,
            city=city,
            market_type=mtype,
            date=date_part,
            actual_temp_f=actual_temp,
        )

        for m in mkts:
            sm = SettledMarket(
                ticker=m.get('ticker', ''),
                event_ticker=et,
                result=m.get('result', 'no'),
                floor_strike=float(m.get('floor_strike', 0)),
                cap_strike=float(m['cap_strike']) if m.get('cap_strike') else None,
                strike_type=m.get('strike_type', 'greater'),
                yes_sub_title=m.get('yes_sub_title', ''),
                volume=m.get('volume', 0),
                expiration_value=actual_temp,
                is_winner=(m.get('result') == 'yes'),
            )
            se.markets.append(sm)

        settled.append(se)
        if (i + 1) % 20 == 0:
            print(f"  Fetched {i+1}/{len(events_raw)} events...")
        time.sleep(0.15)  # Rate limit

    print(f"  Loaded {len(settled)} events with {sum(len(e.markets) for e in settled)} total brackets")
    return settled


# ── Monte Carlo Backtest ─────────────────────────────────────────────────────

def run_backtest(
    settled_events: List[SettledEvent],
    n_trials: int = 500,
    noaa_sigma: float = 2.5,       # Our NOAA model accuracy
    crowd_sigma: float = 4.0,      # Market consensus accuracy (less precise)
    forecast_error_sigma: float = 2.5,  # How far off our forecast might be
    min_edge: float = 0.08,        # Minimum edge to trade
    max_contracts: int = 10,       # Max contracts per trade
    overround: float = 1.15,       # Market overround factor
    taker_fee: int = 2,            # Taker fee cents per side
    seed: int = 42,
) -> BacktestResult:
    """
    Monte Carlo backtest across all settled events.
    
    For each trial:
      - Simulate NOAA forecast = actual_temp + N(0, forecast_error_sigma)
      - Simulate crowd forecast = actual_temp + N(0, crowd_sigma) (less accurate)
      - Generate market prices from crowd model
      - Apply our model (NOAA sigma) to find edges
      - Trade if edge > min_edge
      - Settle based on actual result
    """
    rng = random.Random(seed)
    
    all_trades: List[Trade] = []
    daily_pnls: Dict[str, List[float]] = defaultdict(list)
    city_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
    conf_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
    
    total_scanned = 0
    
    print(f"\n  Running {n_trials} Monte Carlo trials across {len(settled_events)} events...")
    print(f"  NOAA sigma={noaa_sigma}°F  Crowd sigma={crowd_sigma}°F  Min edge={min_edge*100:.0f}%")
    print(f"  Overround={overround:.2f}  Taker fee={taker_fee}¢\n")

    for trial in range(n_trials):
        trial_pnl = 0
        trial_trades = []

        for event in settled_events:
            actual_temp = event.actual_temp_f

            # Simulate our NOAA forecast (close to actual, but not perfect)
            noaa_forecast = actual_temp + rng.gauss(0, forecast_error_sigma)

            # Simulate crowd/market forecast (less accurate)
            crowd_forecast = actual_temp + rng.gauss(0, crowd_sigma)

            # Build bracket definitions
            brackets = []
            for m in event.markets:
                brackets.append({
                    'ticker': m.ticker,
                    'floor_strike': m.floor_strike,
                    'cap_strike': m.cap_strike,
                    'strike_type': m.strike_type,
                    'is_winner': m.is_winner,
                    'volume': m.volume,
                })

            # Generate market prices from crowd model
            market_asks = generate_market_prices(
                brackets, crowd_forecast, crowd_sigma, overround
            )

            # Compute our model's probabilities
            model_probs = compute_bracket_probs(noaa_forecast, brackets, noaa_sigma)

            total_scanned += len(brackets)

            # Find edges and trade
            for b, model_p in model_probs:
                ask = market_asks.get(b['ticker'], 99)
                market_p = ask / 100.0
                edge = model_p - market_p

                if edge < min_edge:
                    continue
                if ask >= 95:  # Don't buy near certainty
                    continue
                if ask <= 0:
                    continue

                # Determine confidence
                if edge >= 0.20:
                    conf = 'high'
                elif edge >= 0.12:
                    conf = 'medium'
                else:
                    conf = 'low'

                # Only trade medium/high confidence
                if conf == 'low':
                    continue

                # Settlement
                won = b['is_winner']
                pnl = (100 - taker_fee - ask) if won else (-ask)

                trade = Trade(
                    ticker=b['ticker'],
                    city=event.city,
                    entry_price_cents=ask,
                    model_prob=model_p,
                    edge=edge,
                    won=won,
                    pnl_cents=pnl,
                )
                trial_trades.append(trade)
                trial_pnl += pnl

                # Track stats
                city_stats[event.city]['trades'] += 1
                city_stats[event.city]['pnl'] += pnl
                if won:
                    city_stats[event.city]['wins'] += 1

                conf_stats[conf]['trades'] += 1
                conf_stats[conf]['pnl'] += pnl
                if won:
                    conf_stats[conf]['wins'] += 1

        all_trades.extend(trial_trades)
        daily_pnls[trial].append(trial_pnl)

        if (trial + 1) % 100 == 0:
            avg_so_far = sum(t.pnl_cents for t in all_trades) / max(1, len(all_trades))
            wr = sum(1 for t in all_trades if t.won) / max(1, len(all_trades))
            print(f"    Trial {trial+1}/{n_trials}  "
                  f"trades={len(all_trades):,}  "
                  f"win_rate={wr:.1%}  "
                  f"avg_pnl={avg_so_far:.1f}¢/trade")

    # ── Aggregate Results ────────────────────────────────────────────────────

    result = BacktestResult()
    result.total_events = len(settled_events)
    result.total_markets_scanned = total_scanned // n_trials
    result.total_trades = len(all_trades)
    result.winning_trades = sum(1 for t in all_trades if t.won)
    result.losing_trades = result.total_trades - result.winning_trades
    result.total_pnl_cents = sum(t.pnl_cents for t in all_trades)
    result.total_invested_cents = sum(t.entry_price_cents for t in all_trades)

    if result.total_trades > 0:
        result.win_rate = result.winning_trades / result.total_trades
        result.avg_edge = sum(t.edge for t in all_trades) / result.total_trades
        result.avg_pnl_per_trade_cents = result.total_pnl_cents / result.total_trades
        result.roi = result.total_pnl_cents / max(1, result.total_invested_cents)
    
    # Per-trial metrics (each trial = one "day" of trading)
    trial_pnls = []
    for trial in range(n_trials):
        tp = sum(t.pnl_cents for t in all_trades[
            trial * (len(all_trades)//n_trials) : (trial+1) * (len(all_trades)//n_trials)
        ])
        trial_pnls.append(tp)

    # Better: compute per-trial P&L directly
    trial_total_pnls = []
    chunk = len(all_trades) // max(1, n_trials)
    if chunk > 0:
        for i in range(n_trials):
            start = i * chunk
            end = start + chunk
            tp = sum(t.pnl_cents for t in all_trades[start:end])
            trial_total_pnls.append(tp)
    
    result.daily_pnls = trial_total_pnls

    # Sharpe-like ratio: mean / std of per-trial P&L
    if trial_total_pnls and len(trial_total_pnls) > 1:
        mean_pnl = sum(trial_total_pnls) / len(trial_total_pnls)
        var_pnl = sum((x - mean_pnl)**2 for x in trial_total_pnls) / (len(trial_total_pnls) - 1)
        std_pnl = math.sqrt(var_pnl) if var_pnl > 0 else 1
        result.sharpe_ratio = mean_pnl / std_pnl

    # Max drawdown (cumulative P&L)
    cum = 0
    peak = 0
    max_dd = 0
    for tp in trial_total_pnls:
        cum += tp
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    result.max_drawdown_cents = max_dd

    # Trades per day (events span 3 days, so per-trial = per-day)
    n_days = len(set(e.date for e in settled_events))
    result.trades_per_day = (result.total_trades / n_trials) / max(1, n_days)

    result.all_trades = all_trades

    # City breakdown (averaged per trial)
    for city, stats in city_stats.items():
        result.by_city[city] = {
            'trades': stats['trades'] / n_trials,
            'wins': stats['wins'] / n_trials,
            'win_rate': stats['wins'] / max(1, stats['trades']),
            'pnl_cents': stats['pnl'] / n_trials,
        }

    # Confidence breakdown
    for conf, stats in conf_stats.items():
        result.by_confidence[conf] = {
            'trades': stats['trades'] / n_trials,
            'wins': stats['wins'] / n_trials,
            'win_rate': stats['wins'] / max(1, stats['trades']),
            'pnl_cents': stats['pnl'] / n_trials,
            'avg_pnl': stats['pnl'] / max(1, stats['trades']),
        }

    return result


# ── Reporting ────────────────────────────────────────────────────────────────

def print_backtest_report(result: BacktestResult, n_trials: int) -> None:
    """Print comprehensive backtest results."""
    n_days = max(1, len(set(t.city for t in result.all_trades[:100])))  # approximate

    print("\n" + "=" * 80)
    print("  NOAA WEATHER STRATEGY — HISTORICAL BACKTEST RESULTS")
    print("=" * 80)

    print(f"""
  Data:       {result.total_events} settled weather events (Mar 1-3, 2026)
  Trials:     {n_trials} Monte Carlo simulations
  Model:      Normal(NOAA_forecast, sigma=2.5°F) vs crowd sigma ≈ 4.0°F

  ── Performance (averaged per trial = per day set) ────────────────────────

  Trades per trial:      {result.total_trades / n_trials:.1f}
  Win rate:              {result.win_rate:.1%}
  Avg edge at entry:     {result.avg_edge*100:+.1f}%
  Avg P&L per trade:     {result.avg_pnl_per_trade_cents:+.1f}¢
  Total P&L per trial:   {result.total_pnl_cents / n_trials:+.1f}¢  (${result.total_pnl_cents / n_trials / 100:+.2f})
  Total invested/trial:  {result.total_invested_cents / n_trials:.0f}¢  (${result.total_invested_cents / n_trials / 100:.2f})
  ROI per trial:         {result.roi*100:+.1f}%
  Sharpe ratio:          {result.sharpe_ratio:.2f}
  Max drawdown:          {result.max_drawdown_cents:.0f}¢  (${result.max_drawdown_cents/100:.2f})
""")

    # Daily P&L distribution
    if result.daily_pnls:
        sorted_pnls = sorted(result.daily_pnls)
        n = len(sorted_pnls)
        p5 = sorted_pnls[int(n * 0.05)]
        p25 = sorted_pnls[int(n * 0.25)]
        p50 = sorted_pnls[int(n * 0.50)]
        p75 = sorted_pnls[int(n * 0.75)]
        p95 = sorted_pnls[int(n * 0.95)]
        pct_profitable = sum(1 for x in sorted_pnls if x > 0) / n

        print(f"  ── P&L Distribution (per trial) ─────────────────────────────────")
        print(f"    5th percentile:  {p5:+.0f}¢  (${p5/100:+.2f})")
        print(f"    25th percentile: {p25:+.0f}¢  (${p25/100:+.2f})")
        print(f"    Median:          {p50:+.0f}¢  (${p50/100:+.2f})")
        print(f"    75th percentile: {p75:+.0f}¢  (${p75/100:+.2f})")
        print(f"    95th percentile: {p95:+.0f}¢  (${p95/100:+.2f})")
        print(f"    % profitable:    {pct_profitable:.1%}")

    # By confidence level
    if result.by_confidence:
        print(f"\n  ── By Confidence Level ───────────────────────────────────────────")
        for conf in ['high', 'medium', 'low']:
            if conf in result.by_confidence:
                s = result.by_confidence[conf]
                print(f"    {conf.upper():<8s}  trades={s['trades']:5.1f}/trial  "
                      f"win_rate={s['win_rate']:.1%}  "
                      f"avg_pnl={s['avg_pnl']:+.1f}¢  "
                      f"total={s['pnl_cents']:+.0f}¢/trial")

    # Top cities
    if result.by_city:
        print(f"\n  ── Top Cities by P&L ────────────────────────────────────────────")
        sorted_cities = sorted(result.by_city.items(), key=lambda x: x[1]['pnl_cents'], reverse=True)
        for city, s in sorted_cities[:10]:
            print(f"    {city:<6s}  trades={s['trades']:5.1f}/trial  "
                  f"win_rate={s['win_rate']:.1%}  "
                  f"pnl={s['pnl_cents']:+.0f}¢/trial  (${s['pnl_cents']/100:+.2f})")

    # Extrapolation
    avg_daily_pnl = result.total_pnl_cents / n_trials
    days_of_data = len(set(e.date for e in []))  # Will be overridden below

    print(f"\n  ── Projected Performance ─────────────────────────────────────────")
    if avg_daily_pnl > 0:
        # Per-trial represents all events in 3 days
        daily_est = avg_daily_pnl / 3  # divide by actual days
        print(f"    Estimated daily P&L:    {daily_est:+.0f}¢  (${daily_est/100:+.2f})")
        print(f"    Estimated monthly P&L:  {daily_est*30:+.0f}¢  (${daily_est*30/100:+.2f})")
        print(f"    Estimated yearly P&L:   {daily_est*365:+.0f}¢  (${daily_est*365/100:+.2f})")

        # With compounding at different capital levels
        print(f"\n    Capital scaling (1 contract each):")
        for capital in [5, 25, 50, 100, 500]:
            # Approximate: each trade costs ~15-30¢ avg
            avg_cost = result.total_invested_cents / max(1, result.total_trades)
            max_simultaneous = min(capital * 100 / max(1, avg_cost), result.total_trades / n_trials)
            scaled_daily = daily_est * min(1, max_simultaneous / max(1, result.total_trades / n_trials))
            print(f"      ${capital:>3d} capital → ~${scaled_daily*30/100:+.2f}/month")
    else:
        print(f"    Strategy is not profitable under these parameters.")

    print(f"\n  ── Key Assumptions ───────────────────────────────────────────────")
    print(f"    NOAA forecast sigma: 2.5°F (NWS 24hr accuracy)")
    print(f"    Market crowd sigma:  4.0°F (public consensus, less precise)")
    print(f"    Market overround:    15% (sum_ask ≈ 115¢ across brackets)")
    print(f"    Taker fee:           2¢/side")
    print(f"    Min edge to trade:   8% (medium/high confidence only)")
    print("=" * 80)


def save_backtest_results(result: BacktestResult, n_trials: int, filepath: str = "results/backtest.json"):
    """Save detailed backtest results to JSON."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    # Summarize trades (don't save all millions of them)
    trade_summary = {
        'total': result.total_trades,
        'winning': result.winning_trades,
        'losing': result.losing_trades,
        'sample': [
            {
                'ticker': t.ticker,
                'city': t.city,
                'entry': t.entry_price_cents,
                'model_prob': round(t.model_prob, 3),
                'edge': round(t.edge, 3),
                'won': t.won,
                'pnl': t.pnl_cents,
            }
            for t in result.all_trades[:200]
        ],
    }

    report = {
        'total_events': result.total_events,
        'total_markets_scanned': result.total_markets_scanned,
        'n_trials': n_trials,
        'win_rate': round(result.win_rate, 4),
        'avg_edge': round(result.avg_edge, 4),
        'avg_pnl_per_trade_cents': round(result.avg_pnl_per_trade_cents, 2),
        'total_pnl_per_trial': round(result.total_pnl_cents / n_trials, 2),
        'roi': round(result.roi, 4),
        'sharpe_ratio': round(result.sharpe_ratio, 3),
        'max_drawdown_cents': round(result.max_drawdown_cents, 2),
        'by_confidence': result.by_confidence,
        'by_city': {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in result.by_city.items()},
        'pnl_percentiles': {},
        'trades': trade_summary,
    }

    if result.daily_pnls:
        sp = sorted(result.daily_pnls)
        n = len(sp)
        report['pnl_percentiles'] = {
            'p5': round(sp[int(n*0.05)], 2),
            'p25': round(sp[int(n*0.25)], 2),
            'p50': round(sp[int(n*0.50)], 2),
            'p75': round(sp[int(n*0.75)], 2),
            'p95': round(sp[int(n*0.95)], 2),
        }

    with open(filepath, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\n  Detailed results saved to {filepath}")


# ── Sensitivity Analysis ─────────────────────────────────────────────────────

def sensitivity_analysis(settled_events: List[SettledEvent], n_trials: int = 200):
    """Run backtest across different parameter combinations."""
    print("\n" + "=" * 80)
    print("  SENSITIVITY ANALYSIS")
    print("=" * 80)

    configs = [
        # (noaa_sigma, crowd_sigma, min_edge, label)
        (2.0, 3.5, 0.08, "Optimistic (tight NOAA)"),
        (2.5, 4.0, 0.08, "Base case"),
        (3.0, 4.0, 0.08, "NOAA less accurate"),
        (2.5, 3.5, 0.08, "Crowd more accurate"),
        (2.5, 5.0, 0.08, "Crowd less accurate"),
        (2.5, 4.0, 0.05, "Lower edge threshold"),
        (2.5, 4.0, 0.12, "Higher edge threshold"),
        (2.5, 4.0, 0.08, "Base w/ 1.25 overround"),
    ]

    overrounds = [1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.25]

    print(f"\n  {'Config':<30s}  {'Trades':>7s}  {'Win%':>6s}  {'Avg PnL':>8s}  {'ROI':>7s}  {'Sharpe':>7s}")
    print("  " + "-" * 75)

    for (ns, cs, me, label), ovr in zip(configs, overrounds):
        r = run_backtest(
            settled_events,
            n_trials=n_trials,
            noaa_sigma=ns,
            crowd_sigma=cs,
            min_edge=me,
            overround=ovr,
            seed=42,
        )
        trades_per = r.total_trades / n_trials
        print(f"  {label:<30s}  {trades_per:>7.1f}  {r.win_rate:>5.1%}  "
              f"{r.avg_pnl_per_trade_cents:>+7.1f}¢  {r.roi*100:>+6.1f}%  {r.sharpe_ratio:>7.2f}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n  NOAA Weather Strategy Backtest")
    print("  " + "=" * 50)

    cfg = load_config()
    cfg.env = 'prod'
    client = KalshiClient(cfg)

    # Step 1: Fetch settled data
    print("\n  Step 1: Fetching settled weather events from Kalshi...")
    settled = fetch_settled_events(client)

    if not settled:
        print("  ERROR: No settled weather events found!")
        return

    # Print summary of actual temperatures
    print(f"\n  Actual temperatures from settlement:")
    for e in settled[:10]:
        winner = [m.ticker for m in e.markets if m.is_winner]
        print(f"    {e.event_ticker:<35s}  actual={e.actual_temp_f:.0f}°F  "
              f"winner={winner[0] if winner else 'N/A'}")
    if len(settled) > 10:
        print(f"    ... and {len(settled)-10} more events")

    # Step 2: Run main backtest
    print(f"\n  Step 2: Running Monte Carlo backtest...")
    n_trials = 500
    result = run_backtest(
        settled,
        n_trials=n_trials,
        noaa_sigma=2.5,
        crowd_sigma=4.0,
        forecast_error_sigma=2.5,
        min_edge=0.08,
        overround=1.15,
        taker_fee=2,
        seed=42,
    )

    # Step 3: Print report
    print_backtest_report(result, n_trials)

    # Step 4: Save results
    save_backtest_results(result, n_trials)

    # Step 5: Sensitivity analysis
    print(f"\n  Step 3: Running sensitivity analysis...")
    sensitivity_analysis(settled, n_trials=200)

    print("\n  Backtest complete!\n")


if __name__ == "__main__":
    main()
