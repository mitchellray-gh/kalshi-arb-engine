# Kalshi YES+NO Sum Arbitrage Engine

Automated arbitrage engine for [Kalshi](https://kalshi.com) event contracts.

**Strategy:** Buy YES + Buy NO on any market where the combined ask price + fees < $1.00.  
Exactly one side **always** pays $1.00 at settlement → **guaranteed profit**.

```
yes_ask + no_ask + 4¢ fees < 100¢  →  BUY BOTH  →  profit = 100 - sum - 4
```

The only risk is **void resolution** (event cancelled) — fees are lost, but principal is refunded.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/mitchellray-gh/kalshi-arb-engine.git
cd kalshi-arb-engine

# 2. Install
pip install -r requirements.txt

# 3. Estimate profit (no auth needed — uses public market data)
python main.py --estimate

# 4. Monte Carlo simulation
python main.py --sim-returns --trials 2000

# 5. Configure API credentials (for live trading)
cp .env.example .env
# Edit .env with your Kalshi API key ID and private key path

# 6. One-shot scan (requires auth)
python main.py --scan --env demo

# 7. Start the live engine loop
python main.py --env demo
```

## CLI Reference

| Command | Auth? | Description |
|---------|-------|-------------|
| `python main.py --estimate` | No | Scan all live markets, show arb opportunities + profit table |
| `python main.py --sim-returns` | No | Monte Carlo simulate returns (default 1000 trials) |
| `python main.py --sim-returns --trials 5000 --seed 42` | No | Reproducible simulation |
| `python main.py --scan` | Yes | One-shot scan, print arb signals, exit |
| `python main.py --balance` | Yes | Show Kalshi account balance |
| `python main.py --report` | No | Print P&L report from `results/positions.json` |
| `python main.py` | Yes | Start continuous arb engine loop |
| `python main.py --dry-run` | Yes | Force dry-run mode (no real orders) |
| `python main.py --env prod` | — | Use production API instead of demo |

## How It Works

```
┌──────────────────────────────────────────────────────────────┐
│  SCAN        Paginate /markets?status=open                   │
│              Parse yes_ask + no_ask for each market           │
│                                                              │
│  DETECT      is_arb = (yes_ask + no_ask + 4) < 100          │
│              Filters: min_profit_cents, min_volume_24h        │
│                                                              │
│  EXECUTE     POST /portfolio/orders  →  BUY YES (limit)      │
│              POST /portfolio/orders  →  BUY NO  (limit)      │
│              Both legs must fill → locked profit              │
│                                                              │
│  SETTLE      One side pays 100¢  →  profit = 100 - cost     │
│              Void → fees lost (2¢/side), principal refunded   │
└──────────────────────────────────────────────────────────────┘
```

### Example

Market `AAPL-25MAR-ABOVE-200`:
- YES ask = 47¢, NO ask = 48¢
- Sum = 95¢ + 4¢ fees = 99¢
- Buy both for 99¢ → one side pays 100¢ → **+1¢ profit per contract**
- 100 contracts = **$1.00 guaranteed profit**

## Configuration

Copy `.env.example` → `.env` and set:

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | — | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA private key `.pem` file |
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `MAX_ORDER_CENTS` | `2500` | Max capital per arb pair (25.00) |
| `MAX_CONTRACTS` | `100` | Max contracts per leg |
| `MAX_OPEN_POSITIONS` | `20` | Max simultaneous open arb positions |
| `MIN_PROFIT_CENTS` | `5` | Min locked profit to execute (0.05) |
| `MIN_VOLUME_24H` | `50` | Min 24h volume to consider |
| `DRY_RUN` | `true` | Set `false` for live trading |
| `SCAN_INTERVAL_SECONDS` | `15` | Seconds between scan cycles |

## Kalshi API Authentication

Kalshi uses RSA-PSS key-pair authentication:

1. Go to [Kalshi API Keys](https://kalshi.com/account/profile) → generate API key
2. Download the private key `.pem` file
3. Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env`

The engine signs each request with: `RSA-PSS(timestamp_ms + HTTP_METHOD + path)`

## Project Structure

```
kalshi-arb-engine/
├── main.py                  # CLI entry point
├── estimate.py              # Profit estimator + Monte Carlo (no auth)
├── engine/
│   ├── __init__.py
│   ├── config.py            # Config from .env
│   ├── client.py            # Kalshi REST client (RSA-PSS auth)
│   ├── scanner.py           # Market scanner + arb detection
│   ├── executor.py          # Order execution (YES + NO legs)
│   ├── positions.py         # Position tracker (JSON persistence)
│   ├── trading_engine.py    # Main scan→detect→execute→settle loop
│   ├── report.py            # P&L report printer
│   └── logger_setup.py      # Logging config
├── results/                 # Positions + logs (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Fees

Kalshi charges ~2¢ per contract per side. For an arb trade:
- Buy YES: 2¢ fee
- Buy NO: 2¢ fee
- **Total round-trip fee: 4¢ per pair**

A signal is only actionable when `yes_ask + no_ask + 4 < 100` (cent sum below 96¢).

## Dependencies

- `requests` — HTTP client
- `cryptography` — RSA-PSS signature generation
- `websockets` — WebSocket streaming (future use)
- `tabulate` — Pretty table output
- `python-dotenv` — `.env` file loading
