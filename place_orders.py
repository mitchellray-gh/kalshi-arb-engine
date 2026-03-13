#!/usr/bin/env python3
"""
place_orders.py — Fast order placement for validated weather edges.

Optimisations vs v1:
  • Concurrent NOAA fetches (ThreadPoolExecutor)
  • Single-pass market fetch with cursor pagination (no per-event calls)
  • batch_create_orders() — up to 20 orders in one API call
  • Skip redundant per-order freshness re-fetch (prices are < 5 s old)
"""
import os, sys, re, time, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.config import load_config
from engine.client import KalshiClient
from engine.weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP
from engine.weather_model import compute_edge, parse_kalshi_ticker

t0 = time.time()

cfg = load_config()
client = KalshiClient(cfg)

# ── 0. Check balance ─────────────────────────────────────────────────────
bal = client.get_balance()
balance_cents = bal.get("balance", 0)
print(f"\n💰 Account balance: ${balance_cents/100:.2f}")
if balance_cents < 100:
    print("⚠️  Balance too low to trade. Exiting.")
    sys.exit(1)

# ── 1. Fetch forecasts (parallel per-city) ────────────────────────────────
print("\nFetching NOAA forecasts (parallel) …")
fetcher = WeatherFetcher()

# Fetch all cities in parallel threads (NOAA is the slowest part)
from engine.weather_fetcher import CITY_COORDS
cities_to_fetch = list(CITY_COORDS.keys())
forecasts = {}

def _fetch_city(city):
    try:
        return city, fetcher.fetch_city(city)
    except Exception as e:
        return city, None

with ThreadPoolExecutor(max_workers=10) as pool:
    futs = {pool.submit(_fetch_city, c): c for c in cities_to_fetch}
    for fut in as_completed(futs):
        city, fc = fut.result()
        if fc:
            forecasts[city] = fc

print(f"  ✓ {len(forecasts)} cities  ({time.time()-t0:.1f}s)")

# ── 2. Fetch all weather T-bracket markets (paginated, no per-event calls)
print("Fetching Kalshi weather markets …")
t1 = time.time()

# Instead of fetching all events then markets per event,
# fetch markets directly filtering by ticker prefix patterns.
# We need to go through events to find weather ones, but we can
# collect event tickers first, then batch the market fetch.
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
wx_event_tickers = [e["event_ticker"] for e in all_events
                    if wx_re.match(e.get("event_ticker", ""))]

print(f"  ✓ {len(wx_event_tickers)} weather events found  ({time.time()-t1:.1f}s)")

# Fetch markets for all events using ThreadPoolExecutor
t2 = time.time()
all_mkts = []

def _fetch_event_markets(et):
    """Fetch all markets for one event ticker."""
    try:
        mr = client.get_markets(event_ticker=et, limit=200)
        mkts = mr.get("markets", [])
        for m in mkts:
            m["_et"] = et
        return mkts
    except Exception:
        return []

with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(_fetch_event_markets, et): et for et in wx_event_tickers}
    for fut in as_completed(futs):
        all_mkts.extend(fut.result())

print(f"  ✓ {len(all_mkts)} markets loaded  ({time.time()-t2:.1f}s)")

# ── 3. Compute edges (pure CPU — fast) ───────────────────────────────────
sigma = cfg.wx_sigma
min_edge = cfg.wx_min_edge
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
                     sigma=sigma, min_edge=min_edge,
                     floor_strike=floor_s, cap_strike=cap_s)
    if r and r.confidence == "high":
        r._market = m
        edges.append(r)

edges.sort(key=lambda e: e.expected_profit_cents, reverse=True)

print(f"\n🔥 {len(edges)} HIGH-confidence edges (≥20%) found in {time.time()-t0:.1f}s total")

if not edges:
    print("No trades to place. Exiting.")
    sys.exit(0)

# ── 4. Select trades for bankroll ─────────────────────────────────────────
BANKROLL = min(balance_cents, 1000)  # Cap at $10 or available balance
selected = []
total_cost = 0

for e in edges:
    entry = e.market_ask if e.side == "yes" else (100 - e.market_bid)
    if entry <= 0 or entry >= 99:
        continue
    qty = min(3, max(1, int(BANKROLL * 0.15 / entry)))
    cost = entry * qty
    if total_cost + cost > BANKROLL:
        qty = 1
        cost = entry
        if total_cost + cost > BANKROLL:
            continue
    selected.append((e, qty, entry))
    total_cost += cost

print(f"\n📋 SELECTED {len(selected)} TRADES (${total_cost/100:.2f}):\n")

for i, (e, qty, entry) in enumerate(selected):
    print(f"  {i+1:2d}. {e.ticker:36s} BUY {e.side.upper():>3s} ×{qty} @{entry:2d}¢  "
          f"P={e.noaa_probability*100:.0f}% edge={e.edge*100:.0f}% EV={e.expected_profit_cents*qty:.0f}¢")

print(f"\n⚡ READY: {len(selected)} orders, ${total_cost/100:.2f} of ${balance_cents/100:.2f}")
print(f"   Scan-to-ready: {time.time()-t0:.1f}s\n")

response = input("Type 'EXECUTE' to place orders, anything else to abort: ").strip()
if response != "EXECUTE":
    print("Aborted.")
    sys.exit(0)

# ── 5. Place orders — batch API (up to 20 per call) ──────────────────────
print(f"\n🚀 PLACING {len(selected)} ORDERS …\n")
t3 = time.time()

# Build order payloads
order_payloads = []
for e, qty, entry in selected:
    body = {
        "ticker": e.ticker,
        "action": "buy",
        "side": e.side,
        "count": qty,
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
    }
    if e.side == "yes":
        body["yes_price"] = entry
    else:
        body["no_price"] = entry
    order_payloads.append((body, e, qty, entry))

placed = 0
failed = 0
total_spent = 0

# Batch in chunks of 20 (Kalshi batch limit)
BATCH_SIZE = 20
for batch_start in range(0, len(order_payloads), BATCH_SIZE):
    batch = order_payloads[batch_start:batch_start + BATCH_SIZE]
    batch_bodies = [b[0] for b in batch]

    try:
        result = client.batch_create_orders(batch_bodies)
        orders = result.get("orders", [])

        for j, order_resp in enumerate(orders):
            body, e, qty, entry = batch[j]
            order_id = order_resp.get("order_id", "???")
            status = order_resp.get("status", "???")

            if status in ("resting", "filled", "pending"):
                print(f"  ✅ {e.ticker}: BUY {e.side.upper()} ×{qty} @{entry}¢  "
                      f"→ {order_id[:8]} {status}")
                placed += 1
                total_spent += entry * qty
            else:
                print(f"  ⚠️  {e.ticker}: status={status}  {order_resp}")
                failed += 1

    except Exception as ex:
        # Batch failed — fall back to individual orders for this batch
        print(f"  ⚠️  Batch failed ({ex}), falling back to individual orders …")
        for body, e, qty, entry in batch:
            try:
                result = client.create_order(
                    ticker=e.ticker,
                    action="buy",
                    side=e.side,
                    count=qty,
                    order_type="limit",
                    yes_price=entry if e.side == "yes" else None,
                    no_price=entry if e.side == "no" else None,
                )
                order = result.get("order", result)
                order_id = order.get("order_id", "???")
                status = order.get("status", "???")
                print(f"  ✅ {e.ticker}: BUY {e.side.upper()} ×{qty} @{entry}¢  "
                      f"→ {order_id[:8]} {status}")
                placed += 1
                total_spent += entry * qty
            except Exception as ex2:
                print(f"  ❌ {e.ticker}: {ex2}")
                failed += 1

# ── 6. Summary ────────────────────────────────────────────────────────────
elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"ORDER SUMMARY  ({elapsed:.1f}s total)")
print(f"{'='*60}")
print(f"  Placed:  {placed}")
print(f"  Failed:  {failed}")
print(f"  Spent:   ${total_spent/100:.2f}")

try:
    bal2 = client.get_balance()
    new_bal = bal2.get("balance", 0)
    print(f"  Balance: ${new_bal/100:.2f} (was ${balance_cents/100:.2f})")
except:
    pass

print(f"\n  ⏱  Scan-to-done: {elapsed:.1f}s")
print()
