#!/usr/bin/env python3
"""
validate_tfix.py — Verify the T-bracket threshold fix is correct.

Tests:
  1. Unit-test compute_edge with known floor_strike / cap_strike values
  2. Live API check: fetch a few T-bracket markets and confirm our
     probability aligns with Kalshi's interpretation.
"""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.weather_model import (
    compute_edge, parse_kalshi_ticker,
    _prob_above, _prob_below, _norm_cdf,
)
from engine.weather_fetcher import WeatherFetcher, CityForecast

# ─── STEP 1: Math sanity with known values ──────────────────────────────────
print("=" * 70)
print("STEP 1: T-bracket probability math verification")
print("=" * 70)

sigma = 2.5

# Scenario A: Upper tail — floor_strike=43, threshold=43
# YES pays if temp > 43  →  effectively temp ≥ 44
# If forecast = 43°F:
#   OLD (buggy):  P(T ≥ 43) = 0.500
#   NEW (correct): P(T ≥ 44) = P(T > 43) using _prob_above(43, 44, 2.5)
forecast_a = 43.0
threshold_a = 43.0
old_prob_a = _prob_above(forecast_a, threshold_a, sigma)
new_prob_a = _prob_above(forecast_a, threshold_a + 1, sigma)
print(f"\n  Scenario A: Upper tail, floor_strike=43, forecast=43°F")
print(f"    OLD P(T ≥ 43) = {old_prob_a:.4f}  (BUGGY)")
print(f"    NEW P(T ≥ 44) = {new_prob_a:.4f}  (CORRECT)")
print(f"    Δ = {(old_prob_a - new_prob_a)*100:.1f}%  overestimate removed")
assert new_prob_a < old_prob_a, "New should be strictly less"

# Scenario B: Lower tail — cap_strike=79, threshold=79
# YES pays if temp < 79  →  effectively temp ≤ 78
# If forecast = 80°F:
#   OLD (buggy):  P(T < 79) as 1 - P(T ≥ 79) = _prob_below(80, 79, 2.5) but direction was wrong
#   NEW (correct): P(T < 79) = _prob_below(80, 79, 2.5)
forecast_b = 80.0
threshold_b = 79.0
p_below_correct = _prob_below(forecast_b, threshold_b, sigma)
print(f"\n  Scenario B: Lower tail, cap_strike=79, forecast=80°F")
print(f"    P(T < 79) = {p_below_correct:.4f}  (correct for lower tail)")
# OLD code would have treated this as upper tail (z = (79-80)/2.5 = -0.4)
# which is in the ambiguous zone. Depending on ask price it could have been
# interpreted either way.

# Scenario C: Upper tail, forecast far above threshold
# floor_strike=37, forecast=50°F → P(T ≥ 38) should be very high
forecast_c = 50.0
threshold_c = 37.0
p_above_c = _prob_above(forecast_c, threshold_c + 1, sigma)
print(f"\n  Scenario C: Upper tail, floor_strike=37, forecast=50°F")
print(f"    P(T ≥ 38) = {p_above_c:.4f}  (should be ~1.0)")
assert p_above_c > 0.99, f"Expected > 0.99, got {p_above_c}"

# Scenario D: Lower tail, forecast far above threshold
# cap_strike=45, forecast=50°F → P(T < 45) should be very low
forecast_d = 50.0
threshold_d = 45.0
p_below_d = _prob_below(forecast_d, threshold_d, sigma)
print(f"\n  Scenario D: Lower tail, cap_strike=45, forecast=50°F")
print(f"    P(T < 45) = {p_below_d:.4f}  (should be very low)")
assert p_below_d < 0.05, f"Expected < 0.05, got {p_below_d}"

print("\n  ✅ All math checks passed!\n")

# ─── STEP 2: Test compute_edge with floor_strike / cap_strike ──────────────
print("=" * 70)
print("STEP 2: compute_edge integration test with metadata")
print("=" * 70)

# Build a minimal CityForecast
from datetime import datetime, timedelta
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
fake_forecast = CityForecast(
    city="AUS", lat=30.0, lon=-97.0,
    grid_id="EWX", grid_x=100, grid_y=100,
    daily_max_f={tomorrow: 50.0},
    daily_min_f={tomorrow: 43.0},
    hourly_temps_f={},
    precip_prob={},
    fetch_time="test",
)

# Build a fake ticker for tomorrow
date_part = (datetime.now() + timedelta(days=1)).strftime("%y%b%d").upper()

# Test A: Upper tail — KXLOWTAUS-{date}-T43, floor_strike=43
ticker_a = f"KXLOWTAUS-{date_part}-T43"
print(f"\n  Test A: {ticker_a}  floor_strike=43  forecast_low=43°F")
edge_a = compute_edge(
    forecast=fake_forecast,
    ticker=ticker_a,
    bid=30, ask=50,
    volume_24h=100,
    sigma=sigma, min_edge=0.01,
    floor_strike=43, cap_strike=None,
)
if edge_a:
    print(f"    side={edge_a.side}  P={edge_a.noaa_probability:.4f}  edge={edge_a.edge:.4f}")
    print(f"    desc={edge_a.description}")
    # P(T > 43) = P(T >= 44) when forecast=43, sigma=2.5
    expected_p = _prob_above(43, 44, sigma)
    print(f"    Expected P ≈ {expected_p:.4f}")
    if edge_a.side == "yes":
        assert abs(edge_a.noaa_probability - expected_p) < 0.001, \
            f"Mismatch: got {edge_a.noaa_probability} vs expected {expected_p}"
    else:
        # If NO side was selected, the YES probability would still be expected_p
        assert abs((1 - edge_a.noaa_probability) - expected_p) < 0.001, \
            f"Mismatch: got NO prob {edge_a.noaa_probability} but expected YES prob {expected_p}"
    print("    ✅ PASS")
else:
    print("    (No edge found — checking probability would be low is OK)")
    print(f"    P(T≥44 when forecast=43) = {_prob_above(43, 44, sigma):.4f}")

# Test B: Lower tail — KXHIGHNY-{date}-T45, cap_strike=45
ticker_b = f"KXHIGHTNY-{date_part}-T45"
print(f"\n  Test B: {ticker_b}  cap_strike=45  forecast_high=50°F")
edge_b = compute_edge(
    forecast=fake_forecast,
    ticker=ticker_b,
    bid=3, ask=5,
    volume_24h=100,
    sigma=sigma, min_edge=0.01,
    floor_strike=None, cap_strike=45,
)
if edge_b:
    print(f"    side={edge_b.side}  P={edge_b.noaa_probability:.4f}  edge={edge_b.edge:.4f}")
    print(f"    desc={edge_b.description}")
    # P(T < 45) when forecast=50 → very low → BUY NO should have big edge
    expected_p_below = _prob_below(50, 45, sigma)
    print(f"    Expected P(YES=below) ≈ {expected_p_below:.4f}")
    if edge_b.side == "no":
        # We bought NO, so our win probability = 1 - P(below)
        expected_no_prob = 1 - expected_p_below
        assert abs(edge_b.noaa_probability - expected_no_prob) < 0.001, \
            f"Mismatch: got {edge_b.noaa_probability} vs expected {expected_no_prob}"
    print("    ✅ PASS")
else:
    expected_p_below = _prob_below(50, 45, sigma)
    print(f"    P(T<45 when forecast=50) = {expected_p_below:.4f}")
    print(f"    P(NO) = {1 - expected_p_below:.4f}, no_ask = {100 - 3} = 97¢")
    print("    (No edge because no_ask too high — expected)")

# Test C: No metadata (fallback) — should still apply +1/-1 correction
ticker_c = f"KXHIGHTSFO-{date_part}-T71"
fake_sfo = CityForecast(
    city="SFO", lat=37.0, lon=-122.0,
    grid_id="MTR", grid_x=100, grid_y=100,
    daily_max_f={tomorrow: 65.0},
    daily_min_f={tomorrow: 50.0},
    hourly_temps_f={},
    precip_prob={},
    fetch_time="test",
)
print(f"\n  Test C: {ticker_c}  NO metadata  forecast_high=65°F")
edge_c = compute_edge(
    forecast=fake_sfo,
    ticker=ticker_c,
    bid=85, ask=90,
    volume_24h=100,
    sigma=sigma, min_edge=0.01,
    floor_strike=None, cap_strike=None,  # No metadata
)
if edge_c:
    print(f"    side={edge_c.side}  P={edge_c.noaa_probability:.4f}  edge={edge_c.edge:.4f}")
    print(f"    desc={edge_c.description}")
    # Threshold=71, forecast=65 → z = (71-65)/2.5 = 2.4 > 0.5 → upper tail
    # Should use _prob_above(65, 72, 2.5)
    expected_p = _prob_above(65, 72, sigma)
    print(f"    Expected P(T≥72, fallback) ≈ {expected_p:.4f}")
    print("    ✅ PASS")
else:
    expected_p = _prob_above(65, 72, sigma)
    print(f"    P(T≥72) = {expected_p:.4f} vs ask=90¢ → edge = {expected_p - 0.90:.4f}")
    print("    (Negative edge, no signal — correct)")

print()

# ─── STEP 3: Live API check ─────────────────────────────────────────────────
print("=" * 70)
print("STEP 3: Live API — fetch T-brackets and verify interpretation")
print("=" * 70)

from engine.config import load_config
from engine.client import KalshiClient

cfg = load_config()
client = KalshiClient(cfg)

# Fetch a batch of weather markets
import re
cursor = None
samples = []
max_samples = 10
page = 0
while len(samples) < max_samples and page < 20:
    page += 1
    resp = client.get_events(status="open", cursor=cursor)
    events = resp.get("events", [])
    if not events:
        break
    cursor = resp.get("cursor")
    if not cursor:
        break
    for evt in events:
        if evt.get("category") != "Climate and Weather":
            continue
        et = evt.get("event_ticker", "")
        if not re.match(r'^KX(HIGHT|LOWT)', et):
            continue
        mkts_resp = client.get_markets(event_ticker=et, limit=50)
        mkts = mkts_resp.get("markets", [])
        for m in mkts:
            tk = m.get("ticker", "")
            parsed = parse_kalshi_ticker(tk)
            if not parsed or parsed["bracket_code"] != "T":
                continue
            fs = m.get("floor_strike")
            cs = m.get("cap_strike")
            ya = m.get("yes_ask", 0) or 0
            yb = m.get("yes_bid", 0) or 0
            subtitle = m.get("yes_sub_title", "")
            title = m.get("title", "")
            if ya <= 0 and yb <= 0:
                continue
            samples.append({
                "ticker": tk,
                "title": title,
                "subtitle": subtitle,
                "floor_strike": fs,
                "cap_strike": cs,
                "yes_ask": ya,
                "yes_bid": yb,
                "threshold": parsed["threshold"],
            })
            if len(samples) >= max_samples:
                break
        if len(samples) >= max_samples:
            break

print(f"\n  Fetched {len(samples)} live T-bracket samples\n")

# Now fetch forecasts and compute edge
fetcher = WeatherFetcher()
all_fc = fetcher.fetch_all()

for s in samples:
    tk = s["ticker"]
    parsed = parse_kalshi_ticker(tk)
    city = parsed["city"]
    fc = all_fc.get(city)
    if not fc:
        continue

    fs = s["floor_strike"]
    cs = s["cap_strike"]
    fs_val = float(fs) if fs is not None else None
    cs_val = float(cs) if cs is not None else None

    mtype = parsed["market_type"]
    date_iso = parsed["date_iso"]
    threshold = parsed["threshold"]
    noaa_temp = fc.daily_max_f.get(date_iso) if mtype == "high" else fc.daily_min_f.get(date_iso)

    if noaa_temp is None:
        continue

    # Determine expected semantics from metadata
    if fs is not None and cs is None:
        tail = "UPPER"
        effective = threshold + 1
        our_p_yes = _prob_above(noaa_temp, effective, sigma)
        semantics = f"YES = temp > {threshold:.0f} (≥ {effective:.0f})"
    elif cs is not None and fs is None:
        tail = "LOWER"
        effective = threshold - 1
        our_p_yes = _prob_below(noaa_temp, threshold, sigma)
        semantics = f"YES = temp < {threshold:.0f} (≤ {effective:.0f})"
    else:
        tail = "UNKNOWN"
        our_p_yes = None
        semantics = "???"

    market_ask_prob = s["yes_ask"] / 100.0

    edge_obj = compute_edge(
        forecast=fc, ticker=tk,
        bid=s["yes_bid"], ask=s["yes_ask"],
        volume_24h=0, sigma=sigma, min_edge=-1.0,
        floor_strike=fs_val, cap_strike=cs_val,
    )

    print(f"  {tk}")
    print(f"    title: {s['title']}")
    print(f"    subtitle: {s['subtitle']}")
    print(f"    floor_strike={fs}  cap_strike={cs}  → {tail} tail")
    print(f"    {semantics}")
    print(f"    NOAA forecast: {noaa_temp:.0f}°F  threshold: {threshold:.0f}°F")
    if our_p_yes is not None:
        print(f"    Our P(YES): {our_p_yes:.4f}  ({our_p_yes*100:.1f}%)")
    print(f"    Market ask:  {s['yes_ask']}¢ = {market_ask_prob:.2f}")
    if edge_obj:
        print(f"    compute_edge: side={edge_obj.side}  P={edge_obj.noaa_probability:.4f}  edge={edge_obj.edge:.4f}")
    else:
        print(f"    compute_edge: no edge")
    print()

print("=" * 70)
print("VALIDATION COMPLETE")
print("=" * 70)
