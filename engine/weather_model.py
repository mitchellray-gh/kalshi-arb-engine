"""
engine/weather_model.py — Probability model: NOAA forecast → contract value.

Strategy:
  NOAA gives a point forecast for high/low temperature. Real temperatures have
  known forecast error (NWS 24-hour temp forecasts are accurate to ~2-4°F).
  
  We model the actual temperature as:
    T_actual ~ Normal(T_forecast, sigma)
  where sigma = forecast_error_stddev (configurable, default 2.5°F)

  For each Kalshi bracket (e.g., "High in NYC > 41.5°F"), we compute:
    P(T > threshold) = 1 - Phi((threshold - T_forecast) / sigma)
  
  Edge = P(NOAA) - market_ask_price
  If edge > min_edge_threshold, the contract is undervalued → BUY.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .weather_fetcher import CityForecast, KALSHI_CITY_MAP

logger = logging.getLogger(__name__)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using the error function (no scipy needed)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _prob_above(forecast: float, threshold: float, sigma: float) -> float:
    """P(T_actual > threshold) given forecast and uncertainty."""
    if sigma <= 0:
        return 1.0 if forecast > threshold else 0.0
    z = (threshold - forecast) / sigma
    return 1 - _norm_cdf(z)


def _prob_below(forecast: float, threshold: float, sigma: float) -> float:
    """P(T_actual < threshold) given forecast and uncertainty."""
    if sigma <= 0:
        return 1.0 if forecast < threshold else 0.0
    z = (threshold - forecast) / sigma
    return _norm_cdf(z)


def _prob_between(forecast: float, low: float, high: float, sigma: float) -> float:
    """P(low <= T_actual < high) given forecast and uncertainty."""
    return _prob_above(forecast, low, sigma) - _prob_above(forecast, high, sigma)


@dataclass
class MarketEdge:
    """A single trading signal: one contract with computed edge."""
    ticker: str
    event_ticker: str
    city: str
    market_type: str        # "high" or "low" or "rain"
    date: str               # YYYY-MM-DD
    description: str        # Human-readable bracket description
    threshold_f: float      # Temperature threshold in °F
    bracket_type: str       # "above", "below", or "between"
    bracket_low: Optional[float] = None
    bracket_high: Optional[float] = None
    side: str = "yes"              # "yes" = BUY YES, "no" = BUY NO

    # NOAA data
    noaa_forecast_f: float = 0.0
    noaa_sigma_f: float = 2.5
    noaa_probability: float = 0.0     # Our computed probability (0-1)

    # Market data
    market_bid: int = 0               # Bid in cents
    market_ask: int = 0               # Ask in cents
    market_volume_24h: int = 0

    # Edge
    edge: float = 0.0                 # noaa_probability - (ask/100)
    expected_profit_cents: float = 0.0  # If we buy at ask, what's expected profit
    confidence: str = "low"           # "low", "medium", "high"


@dataclass
class WeatherSignal:
    """Collection of edges for a scan cycle."""
    timestamp: str
    total_markets_scanned: int = 0
    edges_found: int = 0
    edges: List[MarketEdge] = field(default_factory=list)


def parse_kalshi_ticker(ticker: str) -> Optional[Dict]:
    """
    Parse a Kalshi weather ticker like 'KXHIGHNY-26MAR05-B41.5' into components.
    
    Returns dict with:
      - event_prefix: 'KXHIGHNY'
      - date_str: '26MAR05'  (YY-Mon-DD)
      - bracket_type: 'B' (between/above) or 'T' (tail/below)
      - threshold: 41.5
      - city: 'NYC'
      - market_type: 'high' or 'low' or 'rain'
    """
    # Match pattern: PREFIX-YYMMMDD-{B|T}###.#
    m = re.match(r'^([A-Z]+)-(\d{2}[A-Z]{3}\d{2})-([BT])(\d+\.?\d*)$', ticker)
    if not m:
        return None

    prefix = m.group(1)
    date_str = m.group(2)
    bracket_code = m.group(3)
    threshold = float(m.group(4))

    # Determine city
    city = KALSHI_CITY_MAP.get(prefix)
    if not city:
        return None

    # Determine market type
    if "HIGH" in prefix:
        market_type = "high"
    elif "LOW" in prefix:
        market_type = "low"
    elif "RAIN" in prefix:
        market_type = "rain"
    else:
        return None

    # Parse date: 26MAR05 → 2026-03-05
    months = {
        "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
        "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
        "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
    }
    yy = date_str[:2]
    mon = date_str[2:5]
    dd = date_str[5:]
    date_iso = f"20{yy}-{months.get(mon, '01')}-{dd}"

    return {
        "event_prefix": prefix,
        "date_str": date_str,
        "bracket_code": bracket_code,
        "threshold": threshold,
        "city": city,
        "market_type": market_type,
        "date_iso": date_iso,
    }


def compute_edge(
    forecast: CityForecast,
    ticker: str,
    bid: int,
    ask: int,
    volume_24h: int,
    event_ticker: str = "",
    sigma: float = 2.5,
    min_edge: float = 0.08,
) -> Optional[MarketEdge]:
    """
    Compute the best edge (BUY YES or BUY NO) between NOAA forecast
    probability and Kalshi market price.

    For each contract we evaluate both sides:
      - BUY YES: edge = P(NOAA) - ask/100
      - BUY NO:  edge = (1 - P(NOAA)) - no_ask/100  where no_ask = 100 - bid

    Returns the side with the largest positive edge, or None if neither
    clears min_edge.

    T-bracket interpretation (BUG FIX):
      - T-type contracts have two variants: upper tail (pays if temp >= threshold)
        and lower tail (pays if temp < threshold).
      - Primary signal: if threshold > noaa_forecast, it is almost certainly the
        UPPER tail (rare event, low probability).  If threshold < noaa_forecast,
        it is the LOWER tail.  We use ask price as a tiebreaker only when
        threshold is within 1 sigma of the forecast (ambiguous zone).
    """
    parsed = parse_kalshi_ticker(ticker)
    if not parsed:
        return None

    city = parsed["city"]
    mtype = parsed["market_type"]
    date_iso = parsed["date_iso"]
    threshold = parsed["threshold"]
    bracket_code = parsed["bracket_code"]

    # Get NOAA forecast temperature for this date
    if mtype == "high":
        noaa_temp = forecast.daily_max_f.get(date_iso)
    elif mtype == "low":
        noaa_temp = forecast.daily_min_f.get(date_iso)
    elif mtype == "rain":
        # Rain requires a separate probability model — skip for now
        return None
    else:
        return None

    if noaa_temp is None:
        logger.debug("No NOAA forecast for %s %s on %s", city, mtype, date_iso)
        return None

    # ── Compute P(YES settles) ─────────────────────────────────────────────
    if bracket_code == "T":
        # Tail contract — determine whether this is the upper or lower tail.
        #
        # FIXED logic (replaces ask-proximity heuristic which misfires near
        # the threshold):
        #   1. If threshold is more than 0.5 sigma ABOVE the forecast →
        #      upper tail (pays if temp >= threshold, rare high event).
        #   2. If threshold is more than 0.5 sigma BELOW the forecast →
        #      lower tail (pays if temp < threshold, rare low event).
        #   3. Ambiguous zone (within 0.5 sigma): fall back to ask-proximity.
        p_above = _prob_above(noaa_temp, threshold, sigma)
        p_below = 1.0 - p_above
        z = (threshold - noaa_temp) / sigma  # positive = threshold above forecast

        if z > 0.5:
            # Threshold clearly above forecast → upper tail
            noaa_prob = p_above
            desc = f"{mtype.title()} ≥ {threshold:.0f}°F"
            btype = "above"
        elif z < -0.5:
            # Threshold clearly below forecast → lower tail
            noaa_prob = p_below
            desc = f"{mtype.title()} < {threshold:.0f}°F"
            btype = "below"
        else:
            # Ambiguous — use ask price as tiebreaker (original heuristic)
            ask_prob = ask / 100.0 if ask > 0 else 0.5
            if abs(p_below - ask_prob) < abs(p_above - ask_prob):
                noaa_prob = p_below
                desc = f"{mtype.title()} < {threshold:.0f}°F"
                btype = "below"
            else:
                noaa_prob = p_above
                desc = f"{mtype.title()} ≥ {threshold:.0f}°F"
                btype = "above"
    else:
        # B-type: bracket contract — pays if temp in [threshold-0.5, threshold+1.5)
        # Each bracket is 2°F wide, centred just below the .5 threshold value.
        bracket_low = threshold - 0.5
        bracket_high = threshold + 1.5
        noaa_prob = _prob_between(noaa_temp, bracket_low, bracket_high, sigma)
        desc = f"{mtype.title()} in [{bracket_low:.0f}°F, {bracket_high:.0f}°F)"
        btype = "between"

    # ── Evaluate both YES and NO sides ────────────────────────────────────
    # YES side: buy YES at ask price
    #   EV = P(yes) * 100 - ask - 2  (taker fee on buy side only; Kalshi pays at settlement)
    # NO side:  buy NO at no_ask = 100 - yes_bid
    #   EV = P(no) * 100 - no_ask - 2
    #
    # We pick whichever side has the better positive edge.

    def _evaluate(p_win: float, entry_price: int, label: str):
        """Return (edge, ev) for buying a contract at entry_price with win probability p_win."""
        if entry_price <= 0 or entry_price >= 99:
            return None, None
        e = p_win - entry_price / 100.0
        ev = p_win * 100 - entry_price - 2  # subtract 2c taker fee
        return e, ev

    yes_ask_price = ask
    no_ask_price = 100 - bid if bid > 0 else 0  # NO ask derived from YES bid

    yes_edge, yes_ev = _evaluate(noaa_prob, yes_ask_price, "YES")
    no_edge, no_ev = _evaluate(1.0 - noaa_prob, no_ask_price, "NO")

    # Pick the best side
    candidates = []
    if yes_edge is not None and yes_edge >= min_edge and yes_ev is not None and yes_ev > 0:
        candidates.append(("yes", yes_edge, yes_ev, yes_ask_price, noaa_prob, desc, btype))
    if no_edge is not None and no_edge >= min_edge and no_ev is not None and no_ev > 0:
        no_desc = desc.replace("≥", "<").replace(" < ", " ≥ ").replace("in [", "outside [") if btype != "between" else f"{mtype.title()} outside [{btype}]"
        # Cleaner NO description
        if btype == "above":
            no_desc = f"{mtype.title()} < {threshold:.0f}°F"
        elif btype == "below":
            no_desc = f"{mtype.title()} ≥ {threshold:.0f}°F"
        else:
            bl = threshold - 0.5
            bh = threshold + 1.5
            no_desc = f"{mtype.title()} outside [{bl:.0f}°F, {bh:.0f}°F)"
        candidates.append(("no", no_edge, no_ev, no_ask_price, 1.0 - noaa_prob, no_desc, btype))

    if not candidates:
        return None

    # Best candidate by edge
    best = max(candidates, key=lambda c: c[1])
    best_side, best_edge, best_ev, best_price, best_prob, best_desc, best_btype = best

    # Confidence level
    if best_edge >= 0.20:
        confidence = "high"
    elif best_edge >= 0.12:
        confidence = "medium"
    else:
        confidence = "low"

    me = MarketEdge(
        ticker=ticker,
        event_ticker=event_ticker,
        city=city,
        market_type=mtype,
        date=date_iso,
        description=best_desc,
        threshold_f=threshold,
        bracket_type=best_btype,
        bracket_low=threshold - 0.5 if best_btype == "between" else None,
        bracket_high=threshold + 1.5 if best_btype == "between" else None,
        side=best_side,
        noaa_forecast_f=noaa_temp,
        noaa_sigma_f=sigma,
        noaa_probability=round(best_prob, 4),
        market_bid=bid,
        market_ask=ask,
        market_volume_24h=volume_24h,
        edge=round(best_edge, 4),
        expected_profit_cents=round(best_ev, 2),
        confidence=confidence,
    )
    logger.info(
        "EDGE: %s [%s]  NOAA=%.0f°F  P=%.1f%%  price=%d¢  edge=%.1f%%  EV=%.1f¢  [%s]",
        ticker, best_side.upper(), noaa_temp, best_prob * 100, best_price,
        best_edge * 100, best_ev, confidence,
    )
    return me
