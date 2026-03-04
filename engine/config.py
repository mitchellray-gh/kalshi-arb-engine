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
    min_profit_cents:   int   = 5         # min locked profit per pair (cents)
    min_volume_24h:     int   = 50

    # Scanning
    scan_interval_seconds: int = 15
    scan_categories:    List[str] = field(default_factory=lambda: ["all"])

    # Profit taking
    take_profit_cents:  int   = 4         # min profit/contract to trigger sell
    stop_loss_pct:      float = 0.50      # sell if value drops below this % of cost
    max_hold_days:      int   = 30        # force sell after N days

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
        min_profit_cents   = int(os.getenv("MIN_PROFIT_CENTS", "5")),
        min_volume_24h     = int(os.getenv("MIN_VOLUME_24H", "50")),
        scan_interval_seconds = int(os.getenv("SCAN_INTERVAL_SECONDS", "15")),
        scan_categories    = cats,
        take_profit_cents  = int(os.getenv("TAKE_PROFIT_CENTS", "4")),
        stop_loss_pct      = float(os.getenv("STOP_LOSS_PCT", "0.50")),
        max_hold_days      = int(os.getenv("MAX_HOLD_DAYS", "30")),
        dry_run            = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        log_level          = os.getenv("LOG_LEVEL", "INFO"),
        log_file           = os.getenv("LOG_FILE", "kalshi_arb.log"),
    )
