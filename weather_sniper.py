#!/usr/bin/env python3
"""
weather_sniper.py — Tight-loop weather market scanner.

Continuously monitors Kalshi weather markets every ~30 seconds looking for
EPHEMERAL liquidity (quotes that briefly appear then vanish).  When a non-zero
bid or ask is detected AND our NOAA probability model shows edge, it logs a
🎯 SNIPE OPPORTUNITY alert.

Key differences from the standard weather scanner:
  • No volume filter — any non-zero quote is interesting
  • NOAA forecasts cached and refreshed every 30 minutes (not every cycle)
  • Cycle time ~30s instead of 5 minutes
  • Extra logging for market-book changes (quote appeared / disappeared)
  • DRY_RUN enforced — never places orders (until user says go)

Usage:
    .venv/bin/python weather_sniper.py
    # Ctrl-C to stop
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

load_dotenv()

from engine.client import KalshiClient
from engine.config import load_config
from engine.logger_setup import setup_logging
from engine.weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP, CityForecast
from engine.weather_model import compute_edge, parse_kalshi_ticker
from engine.weather_trader import CITY_BLACKLIST, CITY_TIER1

import logging

# ── Configuration ─────────────────────────────────────────────────────────────

SCAN_INTERVAL_SECONDS = 30          # time between scan cycles
NOAA_REFRESH_MINUTES  = 30          # re-fetch NOAA forecasts every N minutes
MIN_EDGE              = 0.08        # lower threshold to catch more ephemeral opps
MIN_VOLUME            = 0           # accept ANY quote, even zero-volume markets
ALERT_SOUND           = True        # print BEL character on snipe opportunity

# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class QuoteSnapshot:
    """Tracks the latest quote state for a market."""
    ticker: str
    yes_bid: int = 0
    yes_ask: int = 0
    volume_24h: int = 0
    last_seen: str = ""

@dataclass
class SnipeOpportunity:
    """A detected ephemeral trading opportunity."""
    ticker: str
    city: str
    side: str
    edge: float
    noaa_forecast_f: float
    noaa_probability: float
    market_bid: int
    market_ask: int
    entry_price: int
    expected_profit_cents: float
    confidence: str
    volume_24h: int
    timestamp: str

# ── Sniper class ──────────────────────────────────────────────────────────────

class WeatherSniper:
    """Tight-loop weather market sniper."""

    def __init__(self):
        self.cfg = load_config()
        setup_logging(self.cfg.log_level, "weather_sniper.log")
        self.logger = logging.getLogger("sniper")

        self.client = KalshiClient(self.cfg)
        self.fetcher = WeatherFetcher()

        # Forecast cache
        self._forecasts: Dict[str, CityForecast] = {}  # city_code -> CityForecast
        self._forecast_ts: float = 0.0                  # last NOAA fetch time

        # Quote tracking (detect changes)
        self._prev_quotes: Dict[str, QuoteSnapshot] = {}

        # Stats
        self._cycles = 0
        self._total_markets_checked = 0
        self._total_live_quotes = 0
        self._total_opportunities = 0
        self._opportunities: List[SnipeOpportunity] = []
        self._start_time = time.time()

        # Graceful shutdown
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info("Shutdown signal received")
        self._running = False

    # ── NOAA forecast cache ───────────────────────────────────────────────────

    def _refresh_forecasts_if_needed(self, cities: Set[str]) -> None:
        """Fetch NOAA forecasts, but only every NOAA_REFRESH_MINUTES."""
        age_minutes = (time.time() - self._forecast_ts) / 60
        if self._forecasts and age_minutes < NOAA_REFRESH_MINUTES:
            return  # Cache is still fresh

        self.logger.info("Refreshing NOAA forecasts for %d cities...", len(cities))
        for city in cities:
            if city in self._forecasts and age_minutes < NOAA_REFRESH_MINUTES:
                continue
            try:
                fc = self.fetcher.fetch_city(city)
                if fc is not None:
                    self._forecasts[city] = fc
                    self.logger.debug("  %s: max=%s min=%s", city, fc.daily_max_f, fc.daily_min_f)
                time.sleep(0.3)  # rate-limit courtesy
            except Exception as e:
                self.logger.warning("NOAA fetch failed for %s: %s", city, e)

        self._forecast_ts = time.time()
        self.logger.info("Forecasts cached: %d cities", len(self._forecasts))

    # ── Event discovery ───────────────────────────────────────────────────────

    def _find_weather_events(self) -> List[dict]:
        """Find active weather events within our date window."""
        events = []
        cursor = None
        max_days = self.cfg.wx_max_days_out

        while True:
            resp = self.client.get_events(status="open", cursor=cursor)
            batch = resp.get("events", [])

            for ev in batch:
                cat = ev.get("category", "")
                if "weather" not in cat.lower() and "climate" not in cat.lower():
                    continue

                # Check if any city matches
                ticker = ev.get("event_ticker", "")
                prefix = ticker.rsplit("-", 1)[0] if "-" in ticker else ticker

                city = None
                for pfx, c in KALSHI_CITY_MAP.items():
                    if pfx in prefix:
                        city = c
                        break
                if not city:
                    continue

                # Check date window
                close_date_str = ev.get("close_date") or ev.get("expected_expiration_date", "")
                if close_date_str:
                    try:
                        close_date = datetime.fromisoformat(close_date_str.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        days_out = (close_date - now).total_seconds() / 86400
                        if days_out > max_days or days_out < -0.5:
                            continue
                    except (ValueError, TypeError):
                        pass

                events.append({"event": ev, "city": city, "event_ticker": ticker})

            cursor = resp.get("cursor")
            if not cursor or not batch:
                break

        return events

    # ── Single scan cycle ─────────────────────────────────────────────────────

    def _scan_cycle(self) -> List[SnipeOpportunity]:
        """Run one complete scan of all weather markets."""
        self._cycles += 1
        cycle_start = time.time()
        opportunities = []
        markets_checked = 0
        live_quotes = 0
        quote_changes = 0

        # Step 1: Find weather events
        events = self._find_weather_events()
        if not events:
            self.logger.info("Cycle %d: No weather events found", self._cycles)
            return []

        # Step 2: Determine cities and refresh NOAA if needed
        cities = set(ev["city"] for ev in events)
        self._refresh_forecasts_if_needed(cities)

        # Step 3: Scan every market in every event
        current_quotes: Dict[str, QuoteSnapshot] = {}

        for ev_info in events:
            event = ev_info["event"]
            city = ev_info["city"]
            event_ticker = ev_info["event_ticker"]

            if city in CITY_BLACKLIST:
                continue

            forecast = self._forecasts.get(city)
            if forecast is None:
                continue

            # Get markets for this event
            try:
                resp = self.client.get_markets(event_ticker=event_ticker)
                markets = resp.get("markets", [])
                # Filter for active status locally (like weather_trader does)
                markets = [m for m in markets if m.get("status") == "active"]
            except Exception as e:
                self.logger.warning("Failed to get markets for %s: %s", event_ticker, e)
                continue

            for m in markets:
                ticker = m.get("ticker", "")
                markets_checked += 1

                yes_bid = m.get("yes_bid", 0) or 0
                yes_ask = m.get("yes_ask", 0) or 0
                vol_24h = m.get("volume_24h", 0) or 0

                # Track quote state
                snap = QuoteSnapshot(
                    ticker=ticker,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    volume_24h=vol_24h,
                    last_seen=datetime.now(timezone.utc).isoformat(),
                )
                current_quotes[ticker] = snap

                # Detect quote changes
                prev = self._prev_quotes.get(ticker)
                if prev:
                    if (prev.yes_bid == 0 and prev.yes_ask == 0) and (yes_bid > 0 or yes_ask > 0):
                        self.logger.info(
                            "📈 QUOTE APPEARED: %s  bid=%d¢ ask=%d¢ vol=%d",
                            ticker, yes_bid, yes_ask, vol_24h
                        )
                        quote_changes += 1
                    elif (prev.yes_bid > 0 or prev.yes_ask > 0) and (yes_bid == 0 and yes_ask == 0):
                        self.logger.info(
                            "📉 QUOTE VANISHED: %s  was bid=%d¢ ask=%d¢",
                            ticker, prev.yes_bid, prev.yes_ask
                        )
                        quote_changes += 1
                    elif prev.yes_bid != yes_bid or prev.yes_ask != yes_ask:
                        self.logger.info(
                            "🔄 QUOTE CHANGED: %s  %d/%d → %d/%d",
                            ticker, prev.yes_bid, prev.yes_ask, yes_bid, yes_ask
                        )
                        quote_changes += 1
                else:
                    # First time seeing this market
                    if yes_bid > 0 or yes_ask > 0:
                        self.logger.info(
                            "📈 FIRST SCAN QUOTE: %s  bid=%d¢ ask=%d¢ vol=%d",
                            ticker, yes_bid, yes_ask, vol_24h
                        )

                # Has ANY quote?
                has_quote = yes_bid > 0 or yes_ask > 0
                if has_quote:
                    live_quotes += 1

                # Skip if truly empty
                if not has_quote:
                    continue

                # Step 4: Compute edge against the live quote
                try:
                    # Extract floor_strike / cap_strike for T-bracket correction
                    fs = m.get("floor_strike")
                    cs = m.get("cap_strike")
                    floor_s = float(fs) if fs is not None else None
                    cap_s = float(cs) if cs is not None else None

                    edge = compute_edge(
                        forecast=forecast,
                        ticker=ticker,
                        bid=yes_bid,
                        ask=yes_ask,
                        volume_24h=vol_24h,
                        event_ticker=event_ticker,
                        sigma=self.cfg.wx_sigma,
                        min_edge=MIN_EDGE,
                        floor_strike=floor_s,
                        cap_strike=cap_s,
                    )
                except Exception as e:
                    self.logger.debug("compute_edge error for %s: %s", ticker, e)
                    continue

                if edge:
                    side = getattr(edge, "side", "yes")
                    entry = (100 - edge.market_bid) if side == "no" else edge.market_ask

                    opp = SnipeOpportunity(
                        ticker=edge.ticker,
                        city=city,
                        side=side,
                        edge=edge.edge,
                        noaa_forecast_f=edge.noaa_forecast_f,
                        noaa_probability=edge.noaa_probability,
                        market_bid=edge.market_bid,
                        market_ask=edge.market_ask,
                        entry_price=entry,
                        expected_profit_cents=edge.expected_profit_cents,
                        confidence=edge.confidence,
                        volume_24h=vol_24h,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                    opportunities.append(opp)
                    self._total_opportunities += 1

                    # 🎯 ALERT
                    bell = "\a" if ALERT_SOUND else ""
                    tier = " ★TIER1" if city in CITY_TIER1 else ""
                    print(
                        f"{bell}🎯 SNIPE OPPORTUNITY #{self._total_opportunities}  "
                        f"{edge.ticker}  "
                        f"BUY {side.upper()} @ {entry}¢  "
                        f"edge={edge.edge*100:+.1f}%  "
                        f"EV={edge.expected_profit_cents:+.1f}¢  "
                        f"NOAA={edge.noaa_forecast_f:.0f}°F  "
                        f"P={edge.noaa_probability*100:.0f}%  "
                        f"[{edge.confidence}]{tier}"
                    )

        # Update previous quotes for next cycle
        self._prev_quotes = current_quotes
        self._total_markets_checked += markets_checked
        self._total_live_quotes += live_quotes

        elapsed = time.time() - cycle_start
        self.logger.info(
            "Cycle %d: %d markets, %d live quotes, %d edges, %d quote changes  (%.1fs)",
            self._cycles, markets_checked, live_quotes, len(opportunities), quote_changes, elapsed
        )

        return opportunities

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_opportunities(self) -> None:
        """Save all detected opportunities to JSON."""
        if not self._opportunities:
            return
        os.makedirs("results", exist_ok=True)
        path = "results/sniper_opportunities.json"
        data = [asdict(o) if hasattr(o, '__dataclass_fields__') else o.__dict__
                for o in self._opportunities]
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        self.logger.info("Saved %d opportunities to %s", len(self._opportunities), path)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the sniper loop forever."""
        print("\n" + "=" * 78)
        print("  🔫 WEATHER SNIPER — Ephemeral Quote Hunter")
        print("=" * 78)
        print(f"  Scan interval:   {SCAN_INTERVAL_SECONDS}s")
        print(f"  Min edge:        {MIN_EDGE*100:.0f}%")
        print(f"  Min volume:      {MIN_VOLUME}")
        print(f"  NOAA refresh:    every {NOAA_REFRESH_MINUTES} min")
        print(f"  Mode:            DRY RUN (logging only)")
        print(f"  Max days out:    {self.cfg.wx_max_days_out}")
        print(f"  Sigma:           {self.cfg.wx_sigma}°F")
        print(f"  Blacklisted:     {', '.join(sorted(CITY_BLACKLIST))}")
        print(f"  Tier-1 cities:   {', '.join(sorted(CITY_TIER1))}")
        print("=" * 78)
        print("  Scanning... (Ctrl-C to stop)\n")

        while self._running:
            try:
                opps = self._scan_cycle()
                if opps:
                    self._opportunities.extend(opps)
                    self._save_opportunities()

                # Status line
                elapsed_hrs = (time.time() - self._start_time) / 3600
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(
                    f"  [{ts}] cycle={self._cycles}  "
                    f"markets={self._total_markets_checked}  "
                    f"live_quotes={self._total_live_quotes}  "
                    f"opportunities={self._total_opportunities}  "
                    f"runtime={elapsed_hrs:.1f}h"
                )

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error("Cycle error: %s", e, exc_info=True)
                print(f"  ⚠️  Error: {e}  (retrying in {SCAN_INTERVAL_SECONDS}s)")

            # Wait for next cycle
            for _ in range(SCAN_INTERVAL_SECONDS):
                if not self._running:
                    break
                time.sleep(1)

        # Shutdown summary
        self._print_summary()
        self._save_opportunities()

    def _print_summary(self) -> None:
        """Print session summary on exit."""
        elapsed_hrs = (time.time() - self._start_time) / 3600
        print("\n" + "=" * 78)
        print("  🔫 SNIPER SESSION SUMMARY")
        print("=" * 78)
        print(f"  Runtime:          {elapsed_hrs:.2f} hours")
        print(f"  Scan cycles:      {self._cycles}")
        print(f"  Markets checked:  {self._total_markets_checked}")
        print(f"  Live quotes seen: {self._total_live_quotes}")
        print(f"  Opportunities:    {self._total_opportunities}")

        if self._opportunities:
            print("\n  All detected opportunities:")
            for i, opp in enumerate(self._opportunities, 1):
                tier = " ★" if opp.city in CITY_TIER1 else ""
                print(
                    f"    {i}. {opp.ticker}  BUY {opp.side.upper()} @ {opp.entry_price}¢  "
                    f"edge={opp.edge*100:+.1f}%  EV={opp.expected_profit_cents:+.1f}¢  "
                    f"[{opp.confidence}]{tier}  ({opp.timestamp})"
                )

        print("=" * 78 + "\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sniper = WeatherSniper()
    sniper.run()
