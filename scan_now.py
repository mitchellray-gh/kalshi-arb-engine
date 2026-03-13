#!/usr/bin/env python3
"""Quick one-shot scan: NOAA forecast vs Kalshi prices → find edge."""
import sys, re, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.client import KalshiClient
from engine.config import load_config
from engine.weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP
from engine.weather_model import compute_edge, parse_kalshi_ticker
from datetime import datetime, timezone

cfg = load_config()
client = KalshiClient(cfg)
fetcher = WeatherFetcher()

now = datetime.now(timezone.utc)
today = now.strftime("%Y-%m-%d")
print(f"=== WEATHER EDGE SCAN — {now.strftime('%H:%M UTC')} ({now.hour-5}:{now.strftime('%M')} ET) ===\n")

# ── 1. NOAA forecasts ────────────────────────────────────────────────────
print("Fetching NOAA forecasts for all 20 cities …")
forecasts = fetcher.fetch_all()
print(f"  ✓ {len(forecasts)} cities\n")

print(f"{'City':6s}  {'High':>5s}  {'Low':>5s}")
print("-" * 20)
for c in sorted(forecasts):
    fc = forecasts[c]
    hi = fc.daily_max_f.get(today)
    lo = fc.daily_min_f.get(today)
    print(f"{c:6s}  {hi if hi else 'N/A':>5}  {lo if lo else 'N/A':>5}")
print()

# ── 2. Kalshi weather markets ────────────────────────────────────────────
print("Fetching Kalshi weather markets …")
all_events = []
cursor = None
for _ in range(20):
    params = {"status": "open", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    resp = client._get("/events", params)
    evts = resp.get("events", [])
    all_events.extend(evts)
    cursor = resp.get("cursor")
    if not cursor or not evts:
        break

wx_re = re.compile(r"^KX(HIGHT|LOWT)")
wx_events = [e for e in all_events if wx_re.match(e.get("event_ticker", ""))]

all_mkts = []
for ev in wx_events:
    et = ev["event_ticker"]
    mr = client.get_markets(event_ticker=et, limit=200)
    for m in mr.get("markets", []):
        m["_et"] = et
        all_mkts.append(m)

print(f"  ✓ {len(wx_events)} events, {len(all_mkts)} markets\n")

# ── 3. Compute edge on every market ──────────────────────────────────────
sigma = 2.5
edges = []

for m in all_mkts:
    tk = m["ticker"]

    # Client now normalises: yes_bid/yes_ask are integer cents
    yb = m.get("yes_bid", 0) or 0
    ya = m.get("yes_ask", 0) or 0
    vol = m.get("volume_24h", 0) or 0
    ask_sz = float(m.get("yes_ask_size_fp", 0) or 0)

    if ya <= 0 and yb <= 0:
        continue

    parsed = parse_kalshi_ticker(tk)
    if not parsed or parsed["city"] not in forecasts:
        continue

    fc = forecasts[parsed["city"]]
    # Extract floor_strike / cap_strike for T-bracket correction
    fs = m.get("floor_strike")
    cs = m.get("cap_strike")
    floor_s = float(fs) if fs is not None else None
    cap_s = float(cs) if cs is not None else None
    r = compute_edge(fc, tk, yb, ya, vol, event_ticker=m["_et"],
                     sigma=sigma, min_edge=0.01,
                     floor_strike=floor_s, cap_strike=cap_s)
    if r:
        r._sz = ask_sz          # stash ask-side depth for display
        edges.append(r)

edges.sort(key=lambda e: e.edge, reverse=True)

# ── 4. Display ────────────────────────────────────────────────────────────
hdr = (f"{'TICKER':36s} {'S':>2s} {'NOAA':>5s} {'THR':>4s} "
       f"{'P%':>5s} {'PRC':>4s} {'EDGE':>5s} {'EV¢':>5s} "
       f"{'VOL':>5s} {'DEPTH':>6s} {'C':>1s}")
print(hdr)
print("=" * len(hdr))

for e in edges:
    p = e.market_ask if e.side == "yes" else (100 - e.market_bid)
    c = "H" if e.confidence == "high" else ("M" if e.confidence == "medium" else "L")
    print(f"{e.ticker:36s} {e.side[0].upper():>2s} {e.noaa_forecast_f:5.0f} "
          f"{e.threshold_f:4.0f} {e.noaa_probability*100:5.1f} {p:4d} "
          f"{e.edge*100:5.1f} {e.expected_profit_cents:5.1f} "
          f"{int(e.market_volume_24h):5d} {e._sz:6.0f} {c}")

h = [e for e in edges if e.confidence == "high"]
m2 = [e for e in edges if e.confidence == "medium"]
lo = [e for e in edges if e.confidence == "low"]
print(f"\n🔥 HIGH edge (≥20%): {len(h)}  |  MED (≥12%): {len(m2)}  |  LOW: {len(lo)}  |  TOTAL: {len(edges)}")

if h:
    print("\n🎯 TOP PICKS — BUY THESE:")
    for e in h[:15]:
        p = e.market_ask if e.side == "yes" else (100 - e.market_bid)
        print(f"\n  ➤ {e.ticker}")
        print(f"    BUY {e.side.upper()} @ {p}¢  |  NOAA: {e.noaa_forecast_f:.0f}°F  |  Threshold: {e.threshold_f:.0f}°F")
        print(f"    P(win)={e.noaa_probability*100:.1f}%  Edge={e.edge*100:.1f}%  EV={e.expected_profit_cents:.1f}¢/contract")
        print(f"    Depth: {e._sz:.0f} contracts available  |  24h vol: {int(e.market_volume_24h)}")

if m2:
    print(f"\n📊 MEDIUM EDGE:")
    for e in m2[:10]:
        p = e.market_ask if e.side == "yes" else (100 - e.market_bid)
        print(f"  {e.ticker:36s}  BUY {e.side.upper()} @ {p}¢  edge={e.edge*100:.1f}%  EV={e.expected_profit_cents:.1f}¢")
