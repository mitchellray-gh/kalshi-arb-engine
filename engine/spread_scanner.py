"""
engine/spread_scanner.py — Find high-volume, wide-spread markets for market-making.

Scans ALL open Kalshi markets for spread-capture candidates:
  - Both yes_bid and yes_ask quoted (two-sided market)
  - Spread ≥ min_spread (default 2¢)
  - Volume ≥ min_volume (default 500)
  - Preferably short-dated (games settle same day)

These are NOT arb opportunities — they're market-making targets.
Profit comes from buying at bid+1 and selling at ask-1 (maker fee = 0¢).

Primary feed: NBA, NHL, NCAA, EPL, and other sports game markets
  with 2-4¢ spreads and 5K-100K+ daily volume.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .client import KalshiClient
from .config import Config

logger = logging.getLogger(__name__)

# Maker fee is 0¢ on Kalshi.  Taker fee = 2¢/side.
MAKER_FEE = 0
TAKER_FEE = 2


@dataclass
class SpreadTarget:
    """A market identified as a good spread-capture candidate."""
    ticker:         str
    event_ticker:   str
    title:          str
    category:       str
    yes_bid:        int         # best bid (what takers sell at)
    yes_ask:        int         # best ask (what takers buy at)
    spread:         int         # ask - bid
    volume_24h:     int
    open_interest:  int
    close_time:     str = ""
    # Derived
    buy_price:      int = 0     # price we'll bid at (bid+1 to improve)
    sell_price:     int = 0     # price we'll ask at (ask-1 to improve)
    profit_per_rt:  int = 0     # cents profit per round-trip (sell - buy)
    score:          float = 0.0 # volume * profit_per_rt (profitability proxy)

    @property
    def hours_to_close(self) -> float:
        try:
            close = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            delta = (close - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta / 3600)
        except Exception:
            return float("inf")

    def __post_init__(self) -> None:
        if self.spread >= 4:
            # Wide spread: tighten both sides by 1¢
            self.buy_price = self.yes_bid + 1
            self.sell_price = self.yes_ask - 1
        elif self.spread >= 2:
            # Tight spread: buy at bid+1, sell at ask (don't tighten ask)
            self.buy_price = self.yes_bid + 1
            self.sell_price = self.yes_ask
        else:
            # 1¢ spread: not profitable
            self.buy_price = self.yes_bid
            self.sell_price = self.yes_ask
        self.profit_per_rt = self.sell_price - self.buy_price
        self.score = self.volume_24h * max(0, self.profit_per_rt)


@dataclass
class SpreadScanResult:
    """Result of a spread scan cycle."""
    targets:        List[SpreadTarget]
    total_scanned:  int
    two_sided:      int         # markets with both bid+ask
    elapsed_ms:     float


class SpreadScanner:
    """Scan for market-making spread-capture opportunities."""

    def __init__(self, client: KalshiClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg

    def scan(self) -> SpreadScanResult:
        """Scan event markets for spread targets.

        Instead of paginating all 50K+ markets (slow, mostly illiquid),
        we fetch markets from daily-settling ME events (sports, weather, etc.)
        which are where liquidity concentrates.
        """
        t0 = time.perf_counter()

        min_spread = getattr(self._cfg, 'mm_min_spread', 2)
        min_vol = getattr(self._cfg, 'mm_min_volume', 100)

        # Fetch today's and tomorrow's events (where volume is)
        daily_events = self._fetch_daily_events()
        logger.info("SpreadScanner: found %d daily events", len(daily_events))

        # Fetch markets for each event concurrently
        targets: List[SpreadTarget] = []
        total_mkts = 0
        two_sided = 0

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _check_event(ev: dict) -> List[SpreadTarget]:
            et = ev['event_ticker']
            try:
                resp = self._client.get_markets(event_ticker=et, limit=50)
                mkts = resp.get('markets', [])
            except Exception:
                return []

            found = []
            for m in mkts:
                yb = m.get('yes_bid', 0) or 0
                ya = m.get('yes_ask', 0) or 0
                if yb <= 0 or ya <= 0:
                    continue
                spread = ya - yb
                vol = m.get('volume_24h', 0) or 0
                if spread < min_spread or vol < min_vol:
                    continue
                found.append(SpreadTarget(
                    ticker=m['ticker'],
                    event_ticker=m.get('event_ticker', ''),
                    title=(m.get('title', '') or m.get('subtitle', ''))[:60],
                    category=m.get('category', ''),
                    yes_bid=yb,
                    yes_ask=ya,
                    spread=spread,
                    volume_24h=vol,
                    open_interest=m.get('open_interest', 0) or 0,
                    close_time=m.get('close_time', ''),
                ))
            return found

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_check_event, ev): ev for ev in daily_events}
            for fut in as_completed(futs):
                result = fut.result()
                targets.extend(result)

        # Sort by score (volume * profit_per_rt), then by spread
        targets.sort(key=lambda t: (-t.score, -t.volume_24h))

        elapsed = (time.perf_counter() - t0) * 1000

        if targets:
            logger.info(
                "SpreadScanner: %d targets (top: %s spread=%d vol=%d score=%.0f) in %.0fms",
                len(targets), targets[0].ticker, targets[0].spread,
                targets[0].volume_24h, targets[0].score, elapsed,
            )

        return SpreadScanResult(
            targets=targets,
            total_scanned=len(daily_events),
            two_sided=len(targets),
            elapsed_ms=elapsed,
        )

    def _fetch_daily_events(self) -> List[dict]:
        """Fetch events for today and tomorrow — where daily volume lives."""
        now = datetime.now(timezone.utc)
        # Build date patterns for today and next 2 days
        dates = []
        for d in range(3):
            dt = now + __import__('datetime').timedelta(days=d)
            dates.append(dt.strftime('%y%b%d').upper())     # e.g. "26MAR04"
            dates.append(dt.strftime('%yMAR%d'))             # backup pattern

        # De-duplicate
        date_pats = list(set(dates))

        # Paginate all events, filter to daily
        all_events: List[dict] = []
        cursor = None
        for _ in range(30):
            try:
                resp = self._client.get_events(status='open', cursor=cursor, limit=200)
            except Exception as exc:
                logger.error("Event fetch error: %s", exc)
                break
            batch = resp.get('events', [])
            if not batch:
                break
            for ev in batch:
                et = ev['event_ticker']
                # Match daily events by date in ticker
                if any(dp in et for dp in date_pats):
                    all_events.append(ev)
            cursor = resp.get('cursor')
            if not cursor:
                break
            time.sleep(0.2)

        return all_events

    def get_live_quote(self, ticker: str) -> Tuple[int, int]:
        """Fetch fresh bid/ask for a specific market."""
        try:
            data = self._client.get_market(ticker)
            m = data.get('market', data)
            yb = m.get('yes_bid', 0) or 0
            ya = m.get('yes_ask', 0) or 0
            return yb, ya
        except Exception:
            return 0, 0
