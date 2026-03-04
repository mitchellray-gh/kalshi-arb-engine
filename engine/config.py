"""
engine/config.py — Load and validate configuration from .env / environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # Kalshi credentials
    api_key_id:         str   = ""
    private_key_path:   str   = ""
    env:                str   = "demo"    # "demo" or "prod"

    # Trading limits
    max_order_cents:    int   = 2500      # max cost per arb pair in cents
    max_contracts:      int   = 100
    max_open_positions: int   = 20

    # Arb thresholds
    min_profit_cents:   int   = 3         # min locked profit per set (lowered for velocity)
    min_volume_24h:     int   = 0         # accept any liquidity

    # Scanning
    scan_interval_seconds: int = 10       # full arb scan interval (was 15)
    scan_categories:    List[str] = field(default_factory=lambda: ["all"])

    # Rapid profit-taking
    min_scalp_cents:    int   = 1         # min NET profit/contract to scalp (after 4¢ fees)
    trail_stop_cents:   int   = 3         # sell if bid drops this much from peak
    stop_loss_pct:      float = 0.50      # sell if value drops below this % of cost
    max_hold_days:      int   = 14        # force sell after N days (was 30)
    profit_check_seconds: int = 5         # fast cycle for profit checks

    # Liquidity requirements
    require_liquid_legs: bool = True      # ONLY enter arbs where ALL legs have bids

    # Market-making (spread capture)
    mm_enabled:         bool  = True      # enable spread-capture market making
    mm_min_spread:      int   = 2         # min spread (cents) to target
    mm_min_volume:      int   = 100       # min 24h volume for target
    mm_buy_timeout:     int   = 120       # seconds before cancelling unfilled buy
    mm_stop_loss:       int   = 5         # max loss per contract before emergency sell
    mm_max_per_market:  int   = 30        # max % of balance per market
    mm_max_total_exposure: int = 80       # max % of balance total exposure
    mm_check_interval:  int   = 5         # seconds between fill checks
    mm_scan_interval:   int   = 60        # seconds between full spread scans

    # Legacy aliases (for backward compat)
    take_profit_cents:  int   = 1         # mapped to min_scalp_cents

    # Execution
    dry_run:            bool  = True
    log_level:          str   = "INFO"
    log_file:           str   = "kalshi_arb.log"

    @property
    def base_url(self) -> str:
        if self.env == "prod":
            return "https://api.elections.kalshi.com/trade-api/v2"
        return "https://demo-api.kalshi.co/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.env == "prod":
            return "wss://api.elections.kalshi.com/trade-api/ws/v2"
        return "wss://demo-api.kalshi.co/trade-api/ws/v2"

    def validate(self) -> None:
        missing = []
        if not self.api_key_id:
            missing.append("KALSHI_API_KEY_ID")
        if not self.private_key_path:
            missing.append("KALSHI_PRIVATE_KEY_PATH")
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")
        if not os.path.isfile(self.private_key_path):
            raise FileNotFoundError(
                f"Private key file not found: {self.private_key_path}\n"
                "Generate one at: https://kalshi.com/account/profile → API Keys"
            )


def load_config() -> Config:
    """Load config from environment (dotenv should be loaded by caller)."""
    from dotenv import load_dotenv
    load_dotenv()

    cats_raw = os.getenv("SCAN_CATEGORIES", "all")
    cats = [c.strip().lower() for c in cats_raw.split(",") if c.strip()]

    return Config(
        api_key_id         = os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        env                = os.getenv("KALSHI_ENV", "demo").lower(),
        max_order_cents    = int(os.getenv("MAX_ORDER_CENTS", "2500")),
        max_contracts      = int(os.getenv("MAX_CONTRACTS", "100")),
        max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "20")),
        min_profit_cents   = int(os.getenv("MIN_PROFIT_CENTS", "3")),
        min_volume_24h     = int(os.getenv("MIN_VOLUME_24H", "0")),
        scan_interval_seconds = int(os.getenv("SCAN_INTERVAL_SECONDS", "10")),
        scan_categories    = cats,
        min_scalp_cents    = int(os.getenv("MIN_SCALP_CENTS", "1")),
        trail_stop_cents   = int(os.getenv("TRAIL_STOP_CENTS", "3")),
        stop_loss_pct      = float(os.getenv("STOP_LOSS_PCT", "0.50")),
        max_hold_days      = int(os.getenv("MAX_HOLD_DAYS", "14")),
        profit_check_seconds = int(os.getenv("PROFIT_CHECK_SECONDS", "5")),
        take_profit_cents  = int(os.getenv("TAKE_PROFIT_CENTS", "1")),
        require_liquid_legs = os.getenv("REQUIRE_LIQUID_LEGS", "true").lower() in ("true", "1", "yes"),
        mm_enabled         = os.getenv("MM_ENABLED", "true").lower() in ("true", "1", "yes"),
        mm_min_spread      = int(os.getenv("MM_MIN_SPREAD", "2")),
        mm_min_volume      = int(os.getenv("MM_MIN_VOLUME", "100")),
        mm_buy_timeout     = int(os.getenv("MM_BUY_TIMEOUT", "120")),
        mm_stop_loss       = int(os.getenv("MM_STOP_LOSS", "5")),
        mm_max_per_market  = int(os.getenv("MM_MAX_PER_MARKET", "30")),
        mm_max_total_exposure = int(os.getenv("MM_MAX_TOTAL_EXPOSURE", "80")),
        mm_check_interval  = int(os.getenv("MM_CHECK_INTERVAL", "5")),
        mm_scan_interval   = int(os.getenv("MM_SCAN_INTERVAL", "60")),
        dry_run            = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        log_level          = os.getenv("LOG_LEVEL", "INFO"),
        log_file           = os.getenv("LOG_FILE", "kalshi_arb.log"),
    )
