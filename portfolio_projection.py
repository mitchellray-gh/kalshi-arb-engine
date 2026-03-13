#!/usr/bin/env python3
"""portfolio_projection.py — Show P&L projection for all open positions."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from concurrent.futures import ThreadPoolExecutor, as_completed
from engine.config import load_config
from engine.client import KalshiClient
from engine.weather_fetcher import WeatherFetcher, CITY_COORDS
from engine.weather_model import (
    parse_kalshi_ticker, _prob_above, _prob_below, _prob_between,
)

cfg = load_config()
client = KalshiClient(cfg)

# ── 1. Positions ──────────────────────────────────────────────────────────
pos_resp = client.get_positions()
positions = pos_resp.get("market_positions", pos_resp.get("positions", []))
if isinstance(positions, dict):
    positions = list(positions.values())

# ── 2. Fills → cost basis ────────────────────────────────────────────────
fills_all = []
cursor = None
for _ in range(10):
    resp = client.get_fills(limit=200, cursor=cursor)
    batch = resp.get("fills", [])
    fills_all.extend(batch)
    cursor = resp.get("cursor")
    if not cursor or not batch:
        break

# Build cost map from fills: ticker → {side, qty, total_cost_cents}
cost_map = {}
for f in fills_all:
    tk = f.get("ticker", "") or f.get("market_ticker", "")
    side = f.get("side", "")
    count = int(float(f.get("count_fp", 0) or 0))

    # Price in dollar strings: yes_price_dollars / no_price_dollars
    price_cents = 0
    if side == "yes":
        yp = f.get("yes_price_dollars")
        if yp:
            price_cents = int(round(float(yp) * 100))
    elif side == "no":
        np_ = f.get("no_price_dollars")
        if np_:
            price_cents = int(round(float(np_) * 100))

    if tk not in cost_map:
        cost_map[tk] = {"side": side, "qty": 0, "total_cost": 0}
    cost_map[tk]["qty"] += count
    cost_map[tk]["total_cost"] += price_cents * count

# ── 3. NOAA forecasts (parallel) ─────────────────────────────────────────
fetcher = WeatherFetcher()
forecasts = {}
def _fc(c):
    try:
        return c, fetcher.fetch_city(c)
    except:
        return c, None
with ThreadPoolExecutor(max_workers=10) as pool:
    for fut in as_completed([pool.submit(_fc, c) for c in CITY_COORDS]):
        c, fc = fut.result()
        if fc:
            forecasts[c] = fc

# ── 4. Build projections ─────────────────────────────────────────────────
sigma = cfg.wx_sigma
rows = []

for tk, cm in cost_map.items():
    side = cm["side"]
    qty = cm["qty"]
    total_paid = cm["total_cost"]
    if qty == 0:
        continue

    parsed = parse_kalshi_ticker(tk)
    if not parsed:
        continue

    city = parsed["city"]
    mtype = parsed["market_type"]
    date_iso = parsed["date_iso"]
    threshold = parsed["threshold"]
    bcode = parsed["bracket_code"]

    fc = forecasts.get(city)
    if not fc:
        continue

    noaa = fc.daily_max_f.get(date_iso) if mtype == "high" else fc.daily_min_f.get(date_iso)
    if noaa is None:
        continue

    # Fetch floor/cap from live market data
    try:
        mkt_resp = client.get_market(tk)
        mkt = mkt_resp.get("market", mkt_resp)
        fs = mkt.get("floor_strike")
        cs = mkt.get("cap_strike")
    except:
        fs = cs = None

    # P(YES settles) with corrected thresholds
    if bcode == "T":
        if fs is not None and cs is None:
            p_yes = _prob_above(noaa, threshold + 1, sigma)
        elif cs is not None and fs is None:
            p_yes = _prob_below(noaa, threshold, sigma)
        else:
            p_yes = _prob_above(noaa, threshold + 1, sigma)  # fallback
    else:
        p_yes = _prob_between(noaa, threshold - 0.5, threshold + 1.5, sigma)

    p_win = p_yes if side == "yes" else (1.0 - p_yes)

    payout_win = qty * 100
    profit_win = payout_win - total_paid
    loss_lose = -total_paid
    ev = p_win * profit_win + (1 - p_win) * loss_lose

    rows.append({
        "tk": tk, "side": side, "qty": qty, "paid": total_paid,
        "city": city, "mtype": mtype, "date": date_iso,
        "thr": threshold, "noaa": noaa, "p_win": p_win,
        "win": profit_win, "lose": loss_lose, "ev": ev,
        "bcode": bcode,
    })

import time
time.sleep(0.1)  # let rate-limit breathe

rows.sort(key=lambda r: r["ev"], reverse=True)

# ── 5. Print ──────────────────────────────────────────────────────────────
hdr = "{:36s} {:>4s} {:>1s} {:>5s} {:>4s} {:>4s} {:>5s} {:>4s} {:>6s} {:>7s} {:>7s} {:>7s}".format(
    "TICKER", "SIDE", "Q", "COST", "CITY", "TYPE", "NOAA", "THR", "P(WIN)", "IF WIN", "IF LOSE", "E[PnL]"
)
print()
print(hdr)
print("=" * len(hdr))

total_paid_all = 0
total_ev = 0
likely_winners = 0
likely_losers = 0

for r in rows:
    marker = "✅" if r["p_win"] >= 0.50 else "⚠️ "
    if r["p_win"] >= 0.50:
        likely_winners += 1
    else:
        likely_losers += 1
    total_paid_all += r["paid"]
    total_ev += r["ev"]

    line = "{:36s} {:>4s} {:>1d} {:>4d}c {:>4s} {:>4s} {:>4.0f}F {:>4.0f} {:>5.1f}% {:>+6d}c {:>+6d}c {:>+5.0f}c {}".format(
        r["tk"], r["side"].upper(), r["qty"], r["paid"],
        r["city"], r["mtype"], r["noaa"], r["thr"],
        r["p_win"] * 100, r["win"], r["lose"], r["ev"], marker,
    )
    print(line)

print("=" * len(hdr))

# Best / worst / expected
best = sum(r["win"] for r in rows)
worst = sum(r["lose"] for r in rows)
most_likely = sum(r["win"] if r["p_win"] >= 0.5 else r["lose"] for r in rows)

print()
print("PORTFOLIO PROJECTION")
print("─" * 40)
print("  Positions:        {}".format(len(rows)))
print("  Total invested:   {}c (${:.2f})".format(total_paid_all, total_paid_all / 100))
print("  Likely winners:   {} (P(win) ≥ 50%)".format(likely_winners))
print("  Likely losers:    {} (P(win) < 50%)".format(likely_losers))
print()
print("  BEST CASE  (all win):   {:>+6d}c (${:+.2f})".format(best, best / 100))
print("  WORST CASE (all lose):  {:>+6d}c (${:+.2f})".format(worst, worst / 100))
print("  MOST LIKELY outcome:    {:>+6d}c (${:+.2f})".format(most_likely, most_likely / 100))
print("  EXPECTED VALUE (EV):    {:>+6.0f}c (${:+.2f})  ROI: {:.0f}%".format(
    total_ev, total_ev / 100, total_ev / total_paid_all * 100 if total_paid_all else 0))
print()
print('WHAT "HIGH CONFIDENCE" MEANS:')
print("  Edge ≥ 20% = our NOAA probability is 20+ points above the market price.")
print("  Example: We compute P(win) = 88% but market prices it at 1¢ (1%).")
print("  Edge = 87%. The market is massively underpricing the event.")
print()
print("  This does NOT guarantee every trade profits. It means:")
print("  • Our weather model says we have a large statistical advantage")
print("  • Over many bets, we expect strong positive returns")
print("  • Individual trades CAN lose if the weather surprises")
print("  • The ✅ / ⚠️  markers show which way each bet is leaning")
print()
