"""Detailed diagnostic on live arb opportunities — check real fillability."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from engine.config import load_config
from engine.client import KalshiClient

cfg = load_config()
cfg.validate()
client = KalshiClient(cfg)

# The 3 arb events found by scanner
arb_events = ["KXNEWPOPE-70", "KXFTCNEXT-27", "KXPRESTAIWAN-28"]

for et in arb_events:
    resp = client.get_markets(event_ticker=et, limit=50)
    mkts = resp.get("markets", [])
    print(f"\n{'='*70}")
    print(f"EVENT: {et}  ({len(mkts)} markets)")
    print(f"{'='*70}")
    
    sum_yes_ask = 0
    sum_yes_bid = 0
    dead_legs = 0
    all_liquid = True
    
    for m in mkts:
        ya = m.get("yes_ask", 0) or 0
        yb = m.get("yes_bid", 0) or 0
        vol = m.get("volume_24h", 0) or 0
        oi = m.get("open_interest", 0) or 0
        title = (m.get("title") or m.get("subtitle", ""))[:50]
        status = m.get("status", "")
        close = m.get("close_time", "")
        
        if ya > 0:
            sum_yes_ask += ya
            sum_yes_bid += yb
            if yb <= 0:
                dead_legs += 1
                all_liquid = False
        
        liq_flag = "OK" if (yb > 0 and ya > 0) else ("NO_BID" if ya > 0 else "NO_QUOTE")
        print(f"  {m['ticker']:<35} bid={yb:>3}¢  ask={ya:>3}¢  vol={vol:>5}  oi={oi:>5}  {liq_flag}  {title}")
    
    n = sum(1 for m in mkts if (m.get("yes_ask") or 0) > 0)
    fee = n * 2
    profit = 100 - sum_yes_ask - fee
    cost = sum_yes_ask + fee
    roi = profit / cost * 100 if cost > 0 else 0
    
    print(f"\n  SUMMARY:")
    print(f"    Legs with ask: {n}")
    print(f"    sum(yes_ask) = {sum_yes_ask}¢")
    print(f"    sum(yes_bid) = {sum_yes_bid}¢")
    print(f"    Fee: {fee}¢")
    print(f"    Cost per set: {cost}¢ (${cost/100:.2f})")
    print(f"    Profit per set: {profit}¢ (${profit/100:.2f})")
    print(f"    ROI: {roi:.1f}%")
    print(f"    All legs liquid: {all_liquid}")
    print(f"    Dead legs: {dead_legs}")
    
    if sum_yes_bid > 0:
        exit_recovery = max(0, sum_yes_bid - n * 2)
        print(f"    Exit recovery (sell all at bid - fee): {exit_recovery}¢ of {cost}¢ = {exit_recovery/cost*100:.0f}%")
    
    # Capital needed with $10 balance
    if profit > 0 and cost > 0:
        max_sets = 1000 // cost  # 1000 cents = $10
        total_profit = max_sets * profit
        print(f"\n    With $10 balance:")
        print(f"      Max sets: {max_sets}")
        print(f"      Total profit (if all settle): {total_profit}¢ (${total_profit/100:.2f})")
        print(f"      Capital locked: {max_sets * cost}¢")

print("\n\n--- Account Status ---")
bal = client.get_balance()
print(f"Balance: {bal.get('balance', 0)}¢  Portfolio: {bal.get('portfolio_value', 0)}¢")
