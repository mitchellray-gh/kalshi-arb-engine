# Kalshi Multi-Outcome Arbitrage Engine

Automated arbitrage engine for [Kalshi](https://kalshi.com) prediction markets.

## Strategy: Multi-Outcome Event Arbitrage

Kalshi has **mutually exclusive (ME) events** — groups of markets where exactly ONE outcome must settle YES. When the sum of all YES ask prices across outcomes is less than $1.00 (minus fees), buying YES on every outcome guarantees profit.

```
Event: "Who will be the next Pope?"  (7 outcomes, exactly 1 wins)

  Outcome A: YES ask = 5¢
  Outcome B: YES ask = 5¢
  Outcome C: YES ask = 6¢
  Outcome D: YES ask = 9¢
  Outcome E: YES ask = 8¢
  Outcome F: YES ask = 8¢
  Outcome G: YES ask = 7¢
  ─────────────────────────
  Sum = 48¢ + 14¢ fees = 62¢
  One MUST pay 100¢ → profit = 38¢/set (61% ROI)
```

### Exploit Vectors

| # | Vector | Condition | When |
|---|--------|-----------|------|
| 1 | **BUY-ALL-YES** | `sum(yes_ask) + N×fee < 100¢` | ME event, buy YES on every outcome |
| 2 | **BUY-ALL-NO** | `sum(yes_bid) > 100¢ + N×fee` | ME event, buy NO on every outcome |
| 3 | **Single Sum** | `yes_ask + no_ask + 4¢ < 100¢` | Single market (rare — identity enforced) |

**Only risk:** VOID resolution → fees lost, principal refunded.

---

## Live Results

```
11 arb signals  |  2732 ME events  |  11823 markets

  TOTAL LOCKED PROFIT : $129.94
  Capital required    : $272.06
  Guaranteed ROI      : +47.8%

  Top signals:
  • 51st State (7 legs)     sum=21¢  profit=65¢/set  ROI=186%
  • US Recession (6 legs)   sum=26¢  profit=62¢/set  ROI=163%
  • Next Pope (7 legs)      sum=48¢  profit=38¢/set  ROI=61%
  • Next CEO of X (8 legs)  sum=63¢  profit=21¢/set  ROI=27%
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/mitchellray-gh/kalshi-arb-engine.git
cd kalshi-arb-engine

# 2. Install
pip install -r requirements.txt

# 3. Estimate profit (no auth needed — uses public market data)
python main.py --estimate --env prod

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
| `python main.py --estimate` | No | Scan ME events, show arb opportunities + profit table |
| `python main.py --sim-returns` | No | Monte Carlo simulate settlement outcomes (default 1000 trials) |
| `python main.py --sim-returns --trials 5000 --seed 42` | No | Reproducible simulation |
| `python main.py --scan` | Yes | One-shot scan, print arb signals, exit |
| `python main.py --balance` | Yes | Show Kalshi account balance |
| `python main.py --report` | No | Print P&L report from `results/positions.json` |
| `python main.py` | Yes | Start continuous arb engine loop |
| `python main.py --dry-run` | Yes | Force dry-run mode (no real orders) |
| `python main.py --env prod` | — | Use production API instead of demo |

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│  FETCH    Paginate /events → filter mutually_exclusive           │
│           Concurrently fetch /markets per ME event (10 workers)  │
│                                                                  │
│  DETECT   BUY-ALL-YES: sum(yes_ask) + N×fee < 100              │
│           BUY-ALL-NO:  sum(yes_bid) - N×fee > 100              │
│           SINGLE SUM:  yes_ask + no_ask + 4 < 100 (rare)       │
│                                                                  │
│  EXECUTE  POST /portfolio/orders/batched  (up to 20 legs/batch) │
│           Each leg: BUY YES (or NO) at limit price              │
│           All legs must fill → locked profit                     │
│                                                                  │
│  SETTLE   Exactly 1 outcome pays 100¢ → profit collected        │
│           VOID → fees lost, principal refunded                   │
└──────────────────────────────────────────────────────────────────┘
```

## Key Insight: Why This Works

On Kalshi, the **single-market identity** `yes_bid + no_ask = 100` is perfectly enforced — no single-market YES+NO arb exists. However, across **multi-outcome events**, individual market asks are set independently by different traders. When illiquid markets have wide spreads, the sum of YES asks across all outcomes can fall well below 100¢, creating guaranteed arbitrage.

The engine exploits this by:
1. Scanning all ~2700+ mutually-exclusive events
2. Summing YES ask prices across each event's outcomes
3. When sum + fees < 100¢, buying YES on every outcome
4. Exactly one outcome MUST settle YES → pays $1.00 → locked profit

## Configuration

Copy `.env.example` → `.env` and set:

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | — | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA private key `.pem` file |
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `MAX_ORDER_CENTS` | `2500` | Max capital per arb set ($25.00) |
| `MAX_CONTRACTS` | `100` | Max contracts per leg |
| `MAX_OPEN_POSITIONS` | `20` | Max simultaneous open arb positions |
| `MIN_PROFIT_CENTS` | `5` | Min locked profit to execute ($0.05) |
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
│   ├── scanner.py           # ME event scanner + multi-outcome arb detection
│   ├── executor.py          # Multi-leg batch order execution
│   ├── positions.py         # Position tracker (JSON persistence)
│   ├── trading_engine.py    # Scan→detect→execute→settle loop
│   ├── report.py            # P&L report printer
│   └── logger_setup.py      # Logging config
├── results/                 # Positions + logs (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Fee Model

Kalshi charges ~2¢ per contract per side (taker). Maker (limit resting) orders are **free**.

For a multi-outcome arb with N legs:
- Taker fee per set: N × 2¢
- A signal is actionable when `sum(yes_ask) + N×2 < 100`

The engine uses taker fees conservatively. In practice, placing limit orders as maker can reduce fees to zero, improving margins significantly.

## Dependencies

- `requests` — HTTP client with connection pooling
- `cryptography` — RSA-PSS signature generation
- `websockets` — WebSocket streaming (future use)
- `tabulate` — Pretty table output
- `python-dotenv` — `.env` file loading
