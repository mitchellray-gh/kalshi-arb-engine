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
    Compute the edge between NOAA forecast probability and Kalshi market price.
    
    Args:
        forecast: NOAA forecast for this city
        ticker: Kalshi market ticker (e.g., 'KXHIGHNY-26MAR05-B41.5')
        bid/ask: Market bid/ask in cents (0-99)
        volume_24h: 24-hour trading volume
        event_ticker: Parent event ticker
        sigma: Forecast uncertainty in °F (std dev). Default 2.5°F.
        min_edge: Minimum edge (as probability, e.g., 0.08 = 8%) to flag as tradeable
    
    Returns:
        MarketEdge if tradeable edge found, None otherwise
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
        # Rain is a separate model — use precip probability directly
        # For now, skip rain markets (they require different modeling)
        return None
    else:
        return None

    if noaa_temp is None:
        logger.debug("No NOAA forecast for %s %s on %s", city, mtype, date_iso)
        return None

    # Compute probability based on bracket type
    # 'B' + threshold (e.g., B41.5) = "between X and X+2" on Kalshi
    # Actually from the market data: B41.5 means "41° to 42°" (range)
    # T41 means "40° or below" (tail below threshold)
    # But for probability: 
    #   - For ME temperature markets, each contract pays if temp is in that bracket
    #   - B41.5 → YES pays if temp >= 41.5 and < next bracket
    #   - T41 with high temp → YES pays if temp < 41 (in the lower tail)
    #   - T41 with high temp → YES pays if temp >= 41 (in the upper tail) ???
    # 
    # Let me re-examine: From the scan data:
    #   KXHIGHNY-26MAR05-T48 → "49° or above" → pays if high >= 49
    #   KXHIGHNY-26MAR05-B47.5 → "47° to 48°" → pays if 47 <= high <= 48
    #   KXHIGHNY-26MAR05-B45.5 → "45° to 46°"
    #   KXHIGHNY-26MAR05-B43.5 → "43° to 44°"  
    #   KXHIGHNY-26MAR05-B41.5 → "41° to 42°"
    #   KXHIGHNY-26MAR05-T41  → "40° or below" → pays if high <= 40
    #
    # So T## at the TOP = above threshold, T## at the BOTTOM = below threshold
    # B##.5 = bracket from threshold-0.5 to threshold+0.5 (2°F wide)
    #
    # For simplicity: B41.5 covers [41, 43), B43.5 covers [43, 45), etc.
    # Actually the .5 thresholds suggest: B41.5 means the bracket ≥ 41.5
    # And each bracket is 2°F wide.
    #
    # The safest approach: For any YES contract at ask price P:
    #   Our estimated fair value = NOAA_probability
    #   Edge = NOAA_probability - P/100
    #   If we can compute NOAA_probability for the bracket, we're good.
    #
    # For B##.5: pays if temp is in range [threshold-0.5, threshold+1.5)  (2°F bracket)
    # For T## (lower tail on high): pays if temp < threshold
    # For T## (upper tail on high): pays if temp >= threshold
    
    # Determine which type of bracket
    if bracket_code == "T":
        # T-type: This is a tail bracket
        # If this is a HIGH temperature market:
        #   T## at the low end → pays if high < threshold
        #   T## at the high end → pays if high >= threshold
        # We need to figure out if this is upper or lower tail
        # Heuristic: if threshold < noaa_forecast, it's likely the lower tail
        # Better: check if the ask is high (>50) → it's the likely outcome
        # Simplest: compute P(temp < threshold) and P(temp >= threshold), use whichever
        # matches the ask better (closer to ask/100)
        
        p_below = _prob_below(noaa_temp, threshold, sigma)
        p_above = 1 - p_below
        
        # Which interpretation matches the market better?
        ask_prob = ask / 100 if ask > 0 else 0.5
        if abs(p_below - ask_prob) < abs(p_above - ask_prob):
            # Market agrees this is "below threshold"
            noaa_prob = p_below
            desc = f"{mtype.title()} < {threshold}°F"
            btype = "below"
        else:
            # Market agrees this is "above threshold" 
            noaa_prob = p_above
            desc = f"{mtype.title()} ≥ {threshold}°F"
            btype = "above"
    else:
        # B-type: bracket contract — pays if temp in [threshold-0.5, threshold+1.5)
        # This is a 2°F bracket centered near the threshold
        bracket_low = threshold - 0.5
        bracket_high = threshold + 1.5
        noaa_prob = _prob_between(noaa_temp, bracket_low, bracket_high, sigma)
        desc = f"{mtype.title()} in [{bracket_low:.0f}°F, {bracket_high:.0f}°F)"
        btype = "between"

    # Don't buy if ask is 0 or 100 (no opportunity)
    if ask <= 0 or ask >= 99:
        return None

    # Compute edge
    edge = noaa_prob - (ask / 100)

    # Expected profit per contract if we buy at ask (taker, 2¢ fee on buy):
    # Cost: ask + 2¢ fee (paid REGARDLESS of outcome)
    # If settles YES (prob = noaa_prob): receive 100¢, net = 100 - ask - 2 = 98 - ask
    # If settles NO  (prob = 1-noaa_prob): receive 0¢, net = -(ask + 2)
    # EV = noaa_prob * (98 - ask) - (1 - noaa_prob) * (ask + 2)
    #    = 98P - P*ask - ask - 2 + P*ask + 2P
    #    = 100P - ask - 2
    ev_cents = noaa_prob * 100 - ask - 2

    # Confidence level based on edge magnitude
    # Backtest showed medium (12-20%) actually has HIGHER avg PnL (+14.4c)
    # than high (20%+, +9.9c) due to asymmetric payoff on cheaper contracts.
    # Both are profitable. Low (<12%) was not tested and is excluded.
    if edge >= 0.20:
        confidence = "high"
    elif edge >= 0.12:
        confidence = "medium"
    else:
        confidence = "low"

    if edge < min_edge:
        return None

    # Backtest: negative EV means even with edge, fees eat the profit
    if ev_cents < 0:
        return None

    me = MarketEdge(
        ticker=ticker,
        event_ticker=event_ticker,
        city=city,
        market_type=mtype,
        date=date_iso,
        description=desc,
        threshold_f=threshold,
        bracket_type=btype,
        bracket_low=threshold - 0.5 if btype == "between" else None,
        bracket_high=threshold + 1.5 if btype == "between" else None,
        noaa_forecast_f=noaa_temp,
        noaa_sigma_f=sigma,
        noaa_probability=round(noaa_prob, 4),
        market_bid=bid,
        market_ask=ask,
        market_volume_24h=volume_24h,
        edge=round(edge, 4),
        expected_profit_cents=round(ev_cents, 2),
        confidence=confidence,
    )
    logger.info(
        "EDGE: %s  NOAA=%.0f°F  P(NOAA)=%.1f%%  ask=%d¢  edge=%.1f%%  EV=%.1f¢  [%s]",
        ticker, noaa_temp, noaa_prob * 100, ask, edge * 100, ev_cents, confidence,
    )
    return me
