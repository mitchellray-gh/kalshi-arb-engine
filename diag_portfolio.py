"""Check portfolio details via raw API calls."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from engine.config import load_config
from engine.client import KalshiClient

cfg = load_config()
cfg.validate()
client = KalshiClient(cfg)

# Balance
bal = client.get_balance()
print(f"Balance: {bal}")

# Try to get positions / fills
try:
    # Check the API for portfolio positions
    resp = client._get("/portfolio/positions", params={"limit": 100})
    positions = resp.get("market_positions", resp.get("positions", []))
    print(f"\nPositions ({len(positions)}):")
    for p in positions:
        print(f"  {p}")
except Exception as e:
    print(f"Positions error: {e}")

# Try settlements
try:
    resp = client._get("/portfolio/settlements", params={"limit": 20})
    settlements = resp.get("settlements", [])
    print(f"\nSettlements ({len(settlements)}):")
    for s in settlements[:10]:
        print(f"  {s}")
except Exception as e:
    print(f"Settlements error: {e}")

# Try fills / order history
try:
    resp = client._get("/portfolio/fills", params={"limit": 20})
    fills = resp.get("fills", [])
    print(f"\nFills ({len(fills)}):")
    for f in fills[:10]:
        print(f"  {f}")
except Exception as e:
    print(f"Fills error: {e}")
