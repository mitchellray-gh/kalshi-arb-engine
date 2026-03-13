"""Check weather market ORDER BOOKS (not just top of book) and look for 
further-out dates where market makers might be posting quotes.
Also check the actual Kalshi market trade history endpoint."""
import sys, os, time, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(__file__))
from engine.config import load_config
from engine.client import KalshiClient

cfg = load_config()
cfg.validate()
client = KalshiClient(cfg)

print("=" * 70)
print("PART 1: THE WINNING TRADE — REVERSE ENGINEERING")
print("=" * 70)

# Austin low temp Mar 5, settled at YES (temp WAS ≤ 68°F)
# Bought 18 contracts at $0.15 YES on Mar 4 at 19:20 UTC
# That means someone was SELLING at $0.15 (or had a resting ask at 15¢)
# The market closed Mar 5 and settled YES → revenue = 18 × $1.00 = $18
# Cost = 18 × $0.15 = $2.70, fee = $0.17, net = $15.13

print("""
THE AUSTIN TRADE (KXLOWTAUS-26MAR05-T68):
  What: "Will Austin's low temp be ≤ 68°F on March 5?"
  Bought: 18 YES contracts at 15¢ each ($2.70 total)
  Time: March 4, 7:20 PM UTC (1:20 PM Central, day before)
  Result: YES — Austin low was ≤ 68°F
  Revenue: $18.00
  Profit: $15.13 (561% ROI)
  
  KEY INSIGHT: Someone had a 15¢ ask posted on this market.
  The model said YES probability was much higher than 15% → edge.
  This was a TAKER fill — we hit someone's resting limit order.

THE LOSERS:
  KXLOWTMIA-26MAR04-T72: 4 YES at 1¢ ($0.04) — tiny penny bet, lost $0.05
  KXHIGHTSFO-26MAR04-T64: 30 YES at 4¢ ($1.20) — lost $1.29
  
  Both were same-day markets (Mar 4) bought at 19:20 UTC.
  Both expired that same day and went NO.
""")

print("=" * 70)
print("PART 2: CHECK ORDER BOOKS — IS THERE HIDDEN DEPTH?")
print("=" * 70)

# Grab a sample of weather markets and check their order books
resp = client.get_events(status="open", limit=200)
events = resp.get("events", [])
cursor = resp.get("cursor")
while cursor:
    resp = client.get_events(status="open", cursor=cursor, limit=200)
    batch = resp.get("events", [])
    if not batch:
        break
    events.extend(batch)
    cursor = resp.get("cursor")

weather_events = [e for e in events if any(wx in e.get("event_ticker", "").upper() 
    for wx in ["KXHIGHT", "KXLOWT"])]
print(f"\nOpen weather events: {len(weather_events)}")

# Group by date distance
now = datetime.now(timezone.utc)
by_days_out = {}
for ev in weather_events:
    et = ev["event_ticker"]
    close = ev.get("close_time") or ev.get("expected_expiration_time", "")
    if close:
        try:
            close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            days = (close_dt - now).days
            by_days_out.setdefault(days, []).append(ev)
        except:
            by_days_out.setdefault(-1, []).append(ev)

print("\nWeather events by days until expiration:")
for d in sorted(by_days_out.keys()):
    print(f"  {d} days out: {len(by_days_out[d])} events")

# Check order books for a sample of markets at different time horizons
checked_books = 0
books_with_depth = 0

# Check up to 30 weather markets across different dates
sample_markets = []
for ev in weather_events[:15]:  # sample 15 events
    et = ev["event_ticker"]
    try:
        mresp = client.get_markets(event_ticker=et, limit=20)
        mkts = mresp.get("markets", [])
        for m in mkts[:3]:  # check 3 markets per event
            sample_markets.append(m)
    except:
        continue

print(f"\nChecking order books for {len(sample_markets)} sample weather markets...")

for m in sample_markets[:30]:
    ticker = m["ticker"]
    try:
        ob = client.get_orderbook(ticker)
        yes_bids = ob.get("orderbook", {}).get("yes", []) if "orderbook" in ob else []
        no_bids = ob.get("orderbook", {}).get("no", []) if "orderbook" in ob else []
        
        # Also check direct yes/no structure
        if not yes_bids and not no_bids:
            yes_bids = ob.get("yes", [])
            no_bids = ob.get("no", [])
        
        checked_books += 1
        has_depth = bool(yes_bids or no_bids)
        if has_depth:
            books_with_depth += 1
            print(f"\n  *** FOUND DEPTH: {ticker} ***")
            print(f"      YES orders: {yes_bids}")
            print(f"      NO orders:  {no_bids}")
    except Exception as e:
        # Print the raw response to understand the format
        if checked_books < 3:
            print(f"  Order book error for {ticker}: {e}")
    
    time.sleep(0.15)  # rate limit

print(f"\nOrder books checked: {checked_books}")
print(f"Books with ANY depth: {books_with_depth}")


print(f"\n\n{'='*70}")
print("PART 3: CHECK MARKET TRADE HISTORY — WHEN DO THESE MARKETS GET VOLUME?")
print(f"{'='*70}")

# Look at individual market details for trade activity
# The Kalshi API has a /markets/{ticker}/trades endpoint or similar
# Let's check the market data more carefully

# Look at markets expiring TOMORROW (most likely to have activity)
tomorrow = (now + timedelta(days=1)).strftime("%y%b%d").upper()  # e.g. "26MAR13"
tomorrow_alt = (now + timedelta(days=1)).strftime("%Y-%m-%d")

print(f"\nLooking for markets expiring tomorrow ({tomorrow})...")

tomorrow_wx = []
for ev in weather_events:
    et = ev["event_ticker"]
    if tomorrow[:5] in et.upper() or "MAR13" in et.upper():
        tomorrow_wx.append(ev)

if not tomorrow_wx:
    # Try matching by close_time
    for ev in weather_events:
        close = ev.get("close_time") or ev.get("expected_expiration_time", "")
        if "2026-03-13" in close:
            tomorrow_wx.append(ev)

print(f"Tomorrow's weather events: {len(tomorrow_wx)}")
for ev in tomorrow_wx[:5]:
    et = ev["event_ticker"]
    title = ev.get("title", "")[:60]
    print(f"\n  {et}: {title}")
    
    try:
        mresp = client.get_markets(event_ticker=et, limit=20)
        mkts = mresp.get("markets", [])
        for m in mkts[:3]:
            ticker = m["ticker"]
            ya = m.get("yes_ask", 0) or 0
            yb = m.get("yes_bid", 0) or 0
            vol = m.get("volume", 0) or 0
            v24 = m.get("volume_24h", 0) or 0
            oi = m.get("open_interest", 0) or 0
            last = m.get("last_price", 0) or 0
            subtitle = m.get("subtitle", m.get("title", ""))[:40]
            print(f"    {ticker:<40} bid={yb:>3}  ask={ya:>3}  vol={vol:>5}  v24={v24:>5}  OI={oi:>5}  last={last:>3}  {subtitle}")
    except:
        pass


print(f"\n\n{'='*70}")
print("PART 4: CHECK ALL WEATHER — EXTENDED RANGE (7+ days out)")  
print(f"{'='*70}")

# Maybe longer-dated weather markets have more interest
long_dated_wx = []
for ev in weather_events:
    close = ev.get("close_time") or ev.get("expected_expiration_time", "")
    if close:
        try:
            close_dt = datetime.fromisoformat(close.replace("Z", "+00:00"))
            days = (close_dt - now).days
            if days >= 3:
                long_dated_wx.append((days, ev))
        except:
            pass

long_dated_wx.sort(key=lambda x: x[0])
print(f"\nWeather events 3+ days out: {len(long_dated_wx)}")

for days, ev in long_dated_wx[:10]:
    et = ev["event_ticker"]
    title = ev.get("title", "")[:50]
    try:
        mresp = client.get_markets(event_ticker=et, limit=20)
        mkts = mresp.get("markets", [])
        any_activity = any((m.get("volume", 0) or 0) > 0 or (m.get("yes_ask", 0) or 0) > 0 for m in mkts)
        total_vol = sum(m.get("volume", 0) or 0 for m in mkts)
        total_oi = sum(m.get("open_interest", 0) or 0 for m in mkts)
        if any_activity:
            print(f"  *** ACTIVITY: {et} ({days}d out) vol={total_vol} OI={total_oi}")
        else:
            print(f"  Dead: {et} ({days}d out) vol={total_vol} OI={total_oi} — {title}")
    except:
        pass
    time.sleep(0.15)

# Final: try placing a limit order scenario
print(f"\n\n{'='*70}")
print("PART 5: WHAT IF WE'RE THE MARKET MAKER?")
print(f"{'='*70}")
print("""
INSIGHT: Your 3 weather trades on Mar 4 all FILLED as taker orders.
That means someone WAS posting limit orders (resting asks) in those markets.

Current status: 0 quotes on any weather market.
This suggests weather market liquidity is EPHEMERAL:
  - Market makers post quotes at certain times (maybe near close)
  - Quotes get hit or pulled quickly
  - Between active periods, the book is empty

STRATEGY OPTIONS:
  1. SNIPER: Monitor weather markets continuously, pounce when quotes appear
     with model edge > threshold. This is how your Mar 4 trades worked.
     
  2. MAKER: Post YOUR OWN limit orders at prices favorable to your model.
     E.g., if model says P(YES)=80%, post a YES buy at 50¢.
     Wait for someone to sell into you. Risk: capital tied up, may not fill.
     
  3. PENNY LOTTERY: Buy deep OTM contracts at 1-5¢ where the model says
     actual probability is 15-30%. Like the Austin trade: bought at 15¢,
     model probability was likely 70%+. Huge edge, small capital.
""")
