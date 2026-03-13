"""Deep-dive into the winning Austin trade and all historical weather fills.
Figure out: when do weather markets have liquidity? What patterns are tradeable?"""
import sys, os, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
from engine.config import load_config
from engine.client import KalshiClient

cfg = load_config()
cfg.validate()
client = KalshiClient(cfg)

print("=" * 70)
print("PART 1: RECONSTRUCT ALL HISTORICAL WEATHER TRADES")
print("=" * 70)

# Get all fills
resp = client.get_fills(limit=200)
fills = resp.get("fills", [])
print(f"\nTotal fills: {len(fills)}")

# Get settlements
resp2 = client.get_settlements()
settlements = resp2.get("settlements", [])
print(f"Total settlements: {len(settlements)}")

# Identify weather-related fills (tickers with KXHIGHT, KXLOWT, KXHIGHTEMP, KXLOWTEMP patterns)
weather_fills = []
for f in fills:
    t = f.get("ticker", "") or f.get("market_ticker", "")
    if any(wx in t.upper() for wx in ["KXHIGHT", "KXLOWT", "KXHIGHTEMP", "KXLOWTEMP"]):
        weather_fills.append(f)

print(f"\nWeather-related fills: {len(weather_fills)}")
for f in weather_fills:
    t = f.get("ticker", "") or f.get("market_ticker", "")
    side = f.get("side", "?")
    qty = f.get("count_fp", f.get("count", "?"))
    yes_price = f.get("yes_price_dollars", f.get("yes_price", "?"))
    no_price = f.get("no_price_dollars", f.get("no_price", "?"))
    fee = f.get("fee_cost", "?")
    created = f.get("created_time", "?")
    taker = f.get("is_taker", "?")
    print(f"\n  Ticker:     {t}")
    print(f"  Side:       {side}")
    print(f"  Quantity:   {qty}")
    print(f"  YES price:  ${yes_price}")
    print(f"  NO price:   ${no_price}")
    print(f"  Fee:        ${fee}")
    print(f"  Taker:      {taker}")
    print(f"  Time:       {created}")

# Weather settlements
weather_settlements = []
for s in settlements:
    t = s.get("ticker", "")
    if any(wx in t.upper() for wx in ["KXHIGHT", "KXLOWT", "KXHIGHTEMP", "KXLOWTEMP"]):
        weather_settlements.append(s)

print(f"\n\n{'='*70}")
print("PART 2: WEATHER SETTLEMENT RESULTS")
print(f"{'='*70}")
print(f"\nWeather settlements: {len(weather_settlements)}")

total_cost = 0
total_revenue = 0
total_fees = 0

for s in weather_settlements:
    t = s.get("ticker", "")
    result = s.get("market_result", "?")
    revenue = s.get("revenue", 0)
    yes_cost_d = s.get("yes_total_cost_dollars", "0")
    yes_cost = float(yes_cost_d) if yes_cost_d else 0
    yes_qty = s.get("yes_count_fp", "0")
    fee = float(s.get("fee_cost", "0"))
    settled = s.get("settled_time", "?")
    
    total_cost += yes_cost
    total_revenue += revenue / 100  # revenue is in cents
    total_fees += fee
    
    pnl = revenue / 100 - yes_cost - fee
    print(f"\n  Ticker:     {t}")
    print(f"  Result:     {result}")
    print(f"  YES qty:    {yes_qty}")
    print(f"  Cost:       ${yes_cost:.4f}")
    print(f"  Fee:        ${fee:.4f}")
    print(f"  Revenue:    ${revenue/100:.2f}")
    print(f"  P&L:        ${pnl:+.4f}")

print(f"\n  TOTALS:")
print(f"    Total cost:    ${total_cost:.2f}")
print(f"    Total fees:    ${total_fees:.2f}")
print(f"    Total revenue: ${total_revenue:.2f}")
print(f"    Net P&L:       ${total_revenue - total_cost - total_fees:+.2f}")


print(f"\n\n{'='*70}")
print("PART 3: SCAN ALL WEATHER MARKETS FOR ANY LIQUIDITY (non-zero bid/ask)")
print(f"{'='*70}")

# Fetch all weather events
resp3 = client.get_events(status="open", limit=200)
events = resp3.get("events", [])
cursor = resp3.get("cursor")
while cursor:
    resp3 = client.get_events(status="open", cursor=cursor, limit=200)
    batch = resp3.get("events", [])
    if not batch:
        break
    events.extend(batch)
    cursor = resp3.get("cursor")

weather_events = [e for e in events if any(wx in e.get("event_ticker", "").upper() 
    for wx in ["KXHIGHT", "KXLOWT"])]

print(f"\nTotal weather events: {len(weather_events)}")

# Check ALL weather markets for any sign of life
markets_with_bids = []
markets_with_asks = []
markets_with_volume = []
markets_with_oi = []
total_wx_markets = 0

for ev in weather_events:
    et = ev["event_ticker"]
    try:
        mresp = client.get_markets(event_ticker=et, limit=100)
        mkts = mresp.get("markets", [])
    except:
        continue
    
    for m in mkts:
        total_wx_markets += 1
        yb = m.get("yes_bid", 0) or 0
        ya = m.get("yes_ask", 0) or 0
        vol = m.get("volume_24h", 0) or 0
        oi = m.get("open_interest", 0) or 0
        vol_total = m.get("volume", 0) or 0  # total volume, not just 24h
        last = m.get("last_price", 0) or 0
        
        if yb > 0:
            markets_with_bids.append((m["ticker"], yb, ya, vol, oi, vol_total, last))
        if ya > 0:
            markets_with_asks.append((m["ticker"], yb, ya, vol, oi, vol_total, last))
        if vol > 0:
            markets_with_volume.append((m["ticker"], yb, ya, vol, oi, vol_total, last))
        if oi > 0:
            markets_with_oi.append((m["ticker"], yb, ya, vol, oi, vol_total, last))

print(f"Total weather markets scanned: {total_wx_markets}")
print(f"Markets with bids > 0:     {len(markets_with_bids)}")
print(f"Markets with asks > 0:     {len(markets_with_asks)}")
print(f"Markets with 24h vol > 0:  {len(markets_with_volume)}")
print(f"Markets with open int > 0: {len(markets_with_oi)}")

if markets_with_oi:
    print(f"\n  Markets with open interest (someone holds positions):")
    for t, yb, ya, vol, oi, vt, last in sorted(markets_with_oi, key=lambda x: -x[4]):
        print(f"    {t:<40} bid={yb:>3}¢  ask={ya:>3}¢  vol24={vol:>5}  OI={oi:>5}  totalVol={vt:>5}  last={last:>3}¢")

if markets_with_volume:
    print(f"\n  Markets with 24h volume:")
    for t, yb, ya, vol, oi, vt, last in sorted(markets_with_volume, key=lambda x: -x[3]):
        print(f"    {t:<40} bid={yb:>3}¢  ask={ya:>3}¢  vol24={vol:>5}  OI={oi:>5}  last={last:>3}¢")

if markets_with_asks:
    print(f"\n  Markets with active asks:")
    for t, yb, ya, vol, oi, vt, last in sorted(markets_with_asks, key=lambda x: -x[2])[:20]:
        print(f"    {t:<40} bid={yb:>3}¢  ask={ya:>3}¢  vol24={vol:>5}  OI={oi:>5}  last={last:>3}¢")


print(f"\n\n{'='*70}")
print("PART 4: CHECK RECENTLY SETTLED WEATHER MARKETS — DID THEY HAVE VOLUME?")
print(f"{'='*70}")

# Check closed weather events to see what volume looked like
resp4 = client.get_events(status="settled", limit=200)
settled_events = resp4.get("events", [])
settled_wx = [e for e in settled_events if any(wx in e.get("event_ticker", "").upper() 
    for wx in ["KXHIGHT", "KXLOWT"])]

print(f"\nRecently settled weather events: {len(settled_wx)}")

# Sample a few to see their volume/OI
for ev in settled_wx[:10]:
    et = ev["event_ticker"]
    title = ev.get("title", "")[:60]
    try:
        mresp = client.get_markets(event_ticker=et, limit=100)
        mkts = mresp.get("markets", [])
    except:
        continue
    
    total_vol = sum(m.get("volume", 0) or 0 for m in mkts)
    total_oi = sum(m.get("open_interest", 0) or 0 for m in mkts)
    active = sum(1 for m in mkts if (m.get("volume", 0) or 0) > 0)
    
    print(f"\n  {et}")
    print(f"    {title}")
    print(f"    Markets: {len(mkts)}  |  Active (had vol): {active}  |  Total vol: {total_vol}  |  Total OI: {total_oi}")
    
    # Show the ones that actually traded
    for m in mkts:
        v = m.get("volume", 0) or 0
        if v > 0:
            result = m.get("result", "?")
            last = m.get("last_price", 0) or 0
            print(f"      {m['ticker']:<40} vol={v:>5}  last={last:>3}¢  result={result}")
