"""
diag_weather.py — Raw diagnostic: dump NOAA probabilities vs live Kalshi prices.
Shows exactly why edges are (or aren't) found.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.config import load_config
from engine.client import KalshiClient
from engine.weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP
from engine.weather_model import compute_edge, parse_kalshi_ticker, _prob_above, _prob_between

from datetime import datetime, timedelta, timezone

cfg = load_config()
client = KalshiClient(cfg)
fetcher = WeatherFetcher()

now = datetime.now(timezone.utc)
max_days_out = 3

# Generate valid date strings
valid_dates = set()
for d in range(max_days_out + 1):
    dt = now + timedelta(days=d)
    valid_dates.add(dt.strftime("%y%b%d").upper())

print(f"\n  Looking for weather events with dates: {sorted(valid_dates)}")
print(f"  Max days out: {max_days_out}\n")

# Find weather events
weather_events = []
cursor = None
while True:
    resp = client.get_events(status="open", cursor=cursor)
    events = resp.get("events", [])
    if not events:
        break
    for evt in events:
        if evt.get("category") != "Climate and Weather":
            continue
        et = evt.get("event_ticker", "")
        parts = et.split("-")
        if len(parts) < 2:
            continue
        prefix = parts[0]
        if prefix not in KALSHI_CITY_MAP:
            continue
        date_part = parts[1]
        if date_part not in valid_dates:
            continue
        weather_events.append(et)
    cursor = resp.get("cursor")
    if not cursor:
        break

print(f"  Found {len(weather_events)} weather events\n")

# Fetch NOAA for all cities involved
cities_needed = set()
for et in weather_events:
    prefix = et.split("-")[0]
    city = KALSHI_CITY_MAP.get(prefix)
    if city:
        cities_needed.add(city)

print(f"  Fetching NOAA for {len(cities_needed)} cities: {sorted(cities_needed)}")
forecasts = fetcher.fetch_all(list(cities_needed))
print(f"  Got forecasts for {len(forecasts)} cities\n")

# Now dump raw data for each market
print("=" * 130)
print(f"  {'Ticker':<45s}  {'NOAA':>6s}  {'Bracket':>20s}  {'P(YES)':>7s}  {'Ask':>4s}  {'Bid':>4s}  {'Gap':>6s}  {'Vol':>5s}  {'Side':>4s}  {'Edge':>6s}")
print("=" * 130)

sigma = 2.5
total_markets = 0
markets_with_quotes = 0
narrow_gaps = 0  # edge within 5%

for et in sorted(weather_events):
    prefix = et.split("-")[0]
    city = KALSHI_CITY_MAP.get(prefix)
    if not city or city not in forecasts:
        continue

    forecast = forecasts[city]
    
    # Get markets
    resp = client.get_markets(event_ticker=et)
    mkts = resp.get("markets", [])
    
    for m in mkts:
        if m.get("status") != "active":
            continue
        ticker = m.get("ticker", "")
        yes_bid = m.get("yes_bid", 0) or 0
        yes_ask = m.get("yes_ask", 0) or 0
        vol = m.get("volume_24h", 0) or 0
        total_markets += 1
        
        parsed = parse_kalshi_ticker(ticker)
        if not parsed:
            continue
        
        threshold = parsed["threshold"]
        bracket_code = parsed["bracket_code"]
        mtype = parsed["market_type"]
        date_iso = parsed["date_iso"]
        
        # Get NOAA temp
        if mtype == "high":
            noaa_temp = forecast.daily_max_f.get(date_iso)
        elif mtype == "low":
            noaa_temp = forecast.daily_min_f.get(date_iso)
        else:
            continue
        
        if noaa_temp is None:
            print(f"  {ticker:<45s}  {'N/A':>6s}  (no NOAA forecast for {date_iso})")
            continue
        
        # Compute probability
        if bracket_code == "T":
            p_above = _prob_above(noaa_temp, threshold, sigma)
            p_below = 1.0 - p_above
            z = (threshold - noaa_temp) / sigma
            if z > 0.5:
                noaa_prob = p_above
                bracket_desc = f">= {threshold:.0f}°F"
            elif z < -0.5:
                noaa_prob = p_below
                bracket_desc = f"< {threshold:.0f}°F"
            else:
                ask_prob = yes_ask / 100.0 if yes_ask > 0 else 0.5
                if abs(p_below - ask_prob) < abs(p_above - ask_prob):
                    noaa_prob = p_below
                    bracket_desc = f"< {threshold:.0f}°F"
                else:
                    noaa_prob = p_above
                    bracket_desc = f">= {threshold:.0f}°F"
        else:
            bracket_low = threshold - 0.5
            bracket_high = threshold + 1.5
            noaa_prob = _prob_between(noaa_temp, bracket_low, bracket_high, sigma)
            bracket_desc = f"[{bracket_low:.0f}, {bracket_high:.0f})°F"
        
        if yes_ask > 0:
            markets_with_quotes += 1
        
        # Compute edges for both sides
        yes_edge = noaa_prob - (yes_ask / 100.0) if yes_ask > 0 else 0
        no_ask = 100 - yes_bid if yes_bid > 0 else 0
        no_edge = (1 - noaa_prob) - (no_ask / 100.0) if no_ask > 0 else 0
        
        best_side = "YES" if yes_edge >= no_edge else "NO"
        best_edge = max(yes_edge, no_edge)
        
        if abs(best_edge) < 0.05:
            narrow_gaps += 1
        
        # Color coding for edge
        if best_edge >= 0.08:
            marker = "***"
        elif best_edge >= 0.05:
            marker = " * "
        elif best_edge >= 0:
            marker = "   "
        else:
            marker = " - "
        
        print(
            f"  {ticker:<45s}  {noaa_temp:5.1f}F  {bracket_desc:>20s}  "
            f"{noaa_prob:6.1%}  {yes_ask:3d}¢  {yes_bid:3d}¢  "
            f"{best_edge:+5.1%}  {vol:5d}  {best_side:<4s}  {marker}"
        )

print("=" * 130)
print(f"\n  SUMMARY:")
print(f"    Total events:         {len(weather_events)}")
print(f"    Total markets:        {total_markets}")
print(f"    Markets with quotes:  {markets_with_quotes}")
print(f"    Edge < ±5%:           {narrow_gaps}  ({narrow_gaps/max(1,total_markets)*100:.0f}% of markets)")
print(f"    Sigma used:           {sigma}°F")
print(f"\n  If most gaps are <5%, markets are pricing weather efficiently.\n")
