"""
engine/scanner.py — Scan Kalshi markets and build arb-ready snapshots.

Scans all open markets, groups by event, and returns MarketPair objects
with yes_ask + no_ask (or derived from orderbook).

Kalshi prices are in CENTS (1-99).  We keep cents internally.
Settlement = 100 cents ($1.00).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .client import KalshiClient
from .config import Config

logger = logging.getLogger(__name__)

FEE_PER_CONTRACT = 2   # cents per contract per side (Kalshi charges ~$0.02)
FEE_ROUND_TRIP   = FEE_PER_CONTRACT * 2   # buy + settle = 4 cents


@dataclass
class MarketQuote:
    """Current quote for a single Kalshi binary contract."""
    ticker:         str
    event_ticker:   str
    title:          str
    category:       str
    status:         str
    # Prices in cents (1-99)
    yes_bid:        int = 0
    yes_ask:        int = 0
    no_bid:         int = 0
    no_ask:         int = 0
    last_price:     int = 0
    volume_24h:     int = 0
    open_interest:  int = 0
    close_time:     str = ""
    result:         str = ""    # "yes", "no", "void", "" (not settled)

    @property
    def yes_mid(self) -> float:
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2.0
        return float(self.last_price) if self.last_price else 0.0

    @property
    def no_mid(self) -> float:
        if self.no_bid > 0 and self.no_ask > 0:
            return (self.no_bid + self.no_ask) / 2.0
        return 100.0 - self.yes_mid

    @property
    def pair_ask_cost(self) -> int:
        """Total cost to buy YES + NO at the ask (cents)."""
        return self.yes_ask + self.no_ask

    @property
    def locked_profit_cents(self) -> int:
        """Locked profit per pair after fees: 100 - yes_ask - no_ask - 2*fee."""
        return 100 - self.yes_ask - self.no_ask - FEE_ROUND_TRIP

    @property
    def hours_to_expiry(self) -> float:
        try:
            close = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            delta = (close - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta / 3600)
        except Exception:
            return float("inf")

    @property
    def is_arb(self) -> bool:
        """True if buying YES + NO costs less than $1.00 after fees."""
        return (
            self.yes_ask > 0
            and self.no_ask > 0
            and self.locked_profit_cents > 0
        )


@dataclass
class ScanResult:
    markets:     List[MarketQuote]
    arb_signals: List[MarketQuote]   # subset where is_arb == True
    elapsed_ms:  float
    total_scanned: int


class MarketScanner:
    """Scans all Kalshi open markets and identifies YES+NO sum arb opportunities."""

    def __init__(self, client: KalshiClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg

    def scan(self) -> ScanResult:
        t0 = time.perf_counter()
        all_markets: list[MarketQuote] = []
        cursor: str | None = None

        # Paginate through all open markets
        while True:
            resp = self._client.get_markets(
                status="open",
                cursor=cursor,
                limit=200,
            )
            raw_markets = resp.get("markets", [])
            if not raw_markets:
                break

            for m in raw_markets:
                mq = self._parse_market(m)
                if mq:
                    all_markets.append(mq)

            cursor = resp.get("cursor")
            if not cursor:
                break

        # Filter arb signals
        arb_signals = [
            m for m in all_markets
            if m.is_arb
            and m.locked_profit_cents >= self._cfg.min_profit_cents
            and m.volume_24h >= self._cfg.min_volume_24h
        ]

        # Sort by locked profit descending (best arb first)
        arb_signals.sort(key=lambda m: m.locked_profit_cents, reverse=True)

        elapsed = (time.perf_counter() - t0) * 1000

        if arb_signals:
            logger.info(
                "Scan complete: %d markets, %d arb signals (%.0fms)",
                len(all_markets), len(arb_signals), elapsed,
            )
            for sig in arb_signals[:5]:
                logger.info(
                    "  ARB %s  yes_ask=%d  no_ask=%d  sum=%d  profit=%d¢  vol=%d",
                    sig.ticker, sig.yes_ask, sig.no_ask,
                    sig.yes_ask + sig.no_ask, sig.locked_profit_cents,
                    sig.volume_24h,
                )
        else:
            logger.debug(
                "Scan complete: %d markets, 0 arb signals (%.0fms)",
                len(all_markets), elapsed,
            )

        return ScanResult(
            markets=all_markets,
            arb_signals=arb_signals,
            elapsed_ms=elapsed,
            total_scanned=len(all_markets),
        )

    def _parse_market(self, m: dict) -> MarketQuote | None:
        """Parse a raw market dict into a MarketQuote."""
        ticker = m.get("ticker", "")
        if not ticker:
            return None

        # Parse prices — Kalshi provides yes_bid, yes_ask, no_bid, no_ask in cents
        # or as dollar strings (yes_bid_dollars etc.)
        yes_bid = self._price_cents(m, "yes_bid")
        yes_ask = self._price_cents(m, "yes_ask")
        no_bid  = self._price_cents(m, "no_bid")
        no_ask  = self._price_cents(m, "no_ask")

        # Derive from YES if NO not provided
        # In Kalshi: YES bid at X = NO ask at (100-X), YES ask at X = NO bid at (100-X)
        if no_bid == 0 and yes_ask > 0:
            no_bid = 100 - yes_ask
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid

        return MarketQuote(
            ticker        = ticker,
            event_ticker  = m.get("event_ticker", ""),
            title         = m.get("title", m.get("subtitle", "")),
            category      = m.get("category", ""),
            status        = m.get("status", ""),
            yes_bid       = yes_bid,
            yes_ask       = yes_ask,
            no_bid        = no_bid,
            no_ask        = no_ask,
            last_price    = self._price_cents(m, "last_price"),
            volume_24h    = int(m.get("volume_24h", 0)),
            open_interest = int(m.get("open_interest", 0)),
            close_time    = m.get("close_time", ""),
            result        = m.get("result", ""),
        )

    @staticmethod
    def _price_cents(m: dict, key: str) -> int:
        """Extract price in cents — handles both int (cents) and string ($) formats."""
        # Try cents field first
        val = m.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        # Try dollar string field
        dollar_key = key + "_dollars"
        dval = m.get(dollar_key)
        if dval:
            try:
                return int(round(float(dval) * 100))
            except (ValueError, TypeError):
                pass
        return 0
