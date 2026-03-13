#!/usr/bin/env python3
"""
profit_sim.py — Simulate expected profitability for top picks.
Uses corrected T-bracket thresholds (floor_strike/cap_strike).
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.config import load_config
from engine.client import KalshiClient
from engine.weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP
from engine.weather_model import compute_edge, parse_kalshi_ticker

cfg = load_config()
client = KalshiClient(cfg)
fetcher = WeatherFetcher()
forecasts = fetcher.fetch_all()

# ── 1. Fetch all weather markets ──────────────────────────────────────────
all_events = []
cursor = None
for _ in range(20):
    resp = client.get_events(status="open", cursor=cursor)
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

# ── 2. Compute edges ─────────────────────────────────────────────────────
sigma = 2.5
edges = []

for m in all_mkts:
    tk = m["ticker"]
    yb = m.get("yes_bid", 0) or 0
    ya = m.get("yes_ask", 0) or 0
    vol = m.get("volume_24h", 0) or 0

    if ya <= 0 and yb <= 0:
        continue

    parsed = parse_kalshi_ticker(tk)
    if not parsed or parsed["city"] not in forecasts:
        continue

    fc = forecasts[parsed["city"]]
    fs = m.get("floor_strike")
    cs = m.get("cap_strike")
    floor_s = float(fs) if fs is not None else None
    cap_s = float(cs) if cs is not None else None

    r = compute_edge(fc, tk, yb, ya, vol, event_ticker=m["_et"],
                     sigma=sigma, min_edge=0.08,
                     floor_strike=floor_s, cap_strike=cap_s)
    if r:
        r._depth = float(m.get("yes_ask_size_fp", 0) or 0)
        edges.append(r)

edges.sort(key=lambda e: e.expected_profit_cents, reverse=True)

# ── 3. Select trades for $10 bankroll ────────────────────────────────────
BANKROLL = 1000  # cents
HIGH = [e for e in edges if e.confidence == "high"]
MED = [e for e in edges if e.confidence == "medium"]

print(f"\n{'='*70}")
print(f"PROFITABILITY SIMULATION — Corrected T-bracket thresholds")
print(f"{'='*70}")
print(f"\nTotal edges: {len(edges)}  (HIGH: {len(HIGH)}, MED: {len(MED)})")
print(f"Bankroll: ${BANKROLL/100:.2f}")

# Strategy: Buy 1 contract of each HIGH edge pick, prioritised by EV
# If a contract costs less than 5¢, buy 2
selected = []
total_cost = 0

for e in HIGH:
    entry = e.market_ask if e.side == "yes" else (100 - e.market_bid)
    if entry <= 0 or entry >= 99:
        continue
    # How many contracts can we afford? Max 3 per market
    qty = min(3, max(1, int(BANKROLL * 0.15 / entry)))  # ~15% per position max
    cost = entry * qty
    if total_cost + cost > BANKROLL:
        # Try 1 contract
        qty = 1
        cost = entry
        if total_cost + cost > BANKROLL:
            continue
    selected.append((e, qty, cost))
    total_cost += cost

# ── 4. Display ────────────────────────────────────────────────────────────
print(f"\n{'TICKER':36s} {'S':>3s} {'QTY':>3s} {'ENTRY':>5s} {'COST':>5s} "
      f"{'P%':>5s} {'EDGE':>5s} {'EV¢':>6s}")
print("-" * 80)

total_ev = 0
for e, qty, cost in selected:
    entry = e.market_ask if e.side == "yes" else (100 - e.market_bid)
    ev = e.expected_profit_cents * qty
    total_ev += ev
    print(f"{e.ticker:36s} {e.side[0].upper():>3s} {qty:>3d} {entry:>5d}¢ {cost:>5d}¢ "
          f"{e.noaa_probability*100:>5.1f} {e.edge*100:>5.1f} {ev:>6.1f}")

print("-" * 80)
print(f"{'TOTAL':36s} {'':>3s} {len(selected):>3d} {'':>5s} {total_cost:>5d}¢ "
      f"{'':>5s} {'':>5s} {total_ev:>6.1f}")

win_rate_guess = sum(e.noaa_probability if e.side == "yes" else (1 - e.noaa_probability)
                     for e, _, _ in selected) / len(selected) if selected else 0

print(f"\n📊 SIMULATION SUMMARY:")
print(f"  Trades:        {len(selected)}")
print(f"  Total cost:    ${total_cost/100:.2f}")
print(f"  Expected PnL:  ${total_ev/100:.2f}")
print(f"  Expected ROI:  {(total_ev/total_cost)*100:.1f}%" if total_cost > 0 else "N/A")
print(f"  Avg win prob:  {win_rate_guess*100:.1f}%")
print(f"  Avg edge:      {sum(e.edge for e,_,_ in selected)/len(selected)*100:.1f}%")

# Worst-case: all trades lose
worst_loss = total_cost
print(f"\n  ⚠ Worst case (all lose): -${worst_loss/100:.2f}")
# Best-case: all trades win at 100¢
best_pnl = sum(100 * qty - cost for _, qty, cost in selected)
print(f"  🎉 Best case (all win):  +${best_pnl/100:.2f}")

# Kelly-like expected value
print(f"\n  💰 If we bet $10 on these {len(selected)} positions:")
print(f"     Expected return: ${(total_cost + total_ev)/100:.2f}")
print(f"     Net profit:      ${total_ev/100:.2f}")
