"""
engine/weather_trader.py — Weather-data-driven trading engine.

Orchestrates:
  1. Scan Kalshi for weather markets expiring within N days (default: tomorrow)
  2. Fetch NOAA forecasts for relevant cities
  3. Compute edge for each contract using the probability model
  4. Place limit-buy orders on contracts with sufficient edge
  5. Track positions for next-day settlement
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .client import KalshiClient
from .config import Config
from .weather_fetcher import WeatherFetcher, KALSHI_CITY_MAP, CityForecast
from .weather_model import MarketEdge, WeatherSignal, compute_edge, parse_kalshi_ticker

logger = logging.getLogger(__name__)

# Backtest-driven city tiers (Mar 1-3 2026 data, 500 MC trials)
# Tier 1: Win rate >= 39%, positive P&L — get 2x+ sizing
CITY_TIER1 = {"PHIL", "NYC", "LAX", "AUS", "BOS"}
# Blacklist: Win rate <= 10%, negative P&L — skip entirely
CITY_BLACKLIST = {"CHI", "DC"}


class WeatherTrader:
    """NOAA-data-driven weather contract trader."""

    def __init__(self, client: KalshiClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self.fetcher = WeatherFetcher()
        self._forecasts: Dict[str, CityForecast] = {}
        self._last_signal: Optional[WeatherSignal] = None

    # ── Market Discovery ──────────────────────────────────────────────────────

    def _find_weather_events(self, max_days_out: int = 2) -> List[dict]:
        """
        Find all Kalshi weather events that close within max_days_out days.
        Returns list of event dicts with their markets.
        """
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=max_days_out)

        # Date strings we care about (e.g., ["26MAR04", "26MAR05"] for 2 days out)
        valid_dates = set()
        for d in range(max_days_out + 1):
            dt = now + timedelta(days=d)
            valid_dates.add(dt.strftime("%y%b%d").upper())  # e.g., "26MAR04"

        weather_events = []
        cursor = None

        while True:
            resp = self.client.get_events(status="open", cursor=cursor)
            events = resp.get("events", [])
            if not events:
                break

            for evt in events:
                cat = evt.get("category", "")
                title = evt.get("title", "")

                # Filter for weather events
                if cat != "Climate and Weather":
                    continue

                # Only daily high/low/rain (skip monthly events)
                event_ticker = evt.get("event_ticker", "")
                if not event_ticker:
                    continue

                # Check if this event has markets we can parse
                # Event tickers look like KXHIGHNY-26MAR05
                parts = event_ticker.split("-")
                if len(parts) < 2:
                    continue

                prefix = parts[0]
                if prefix not in KALSHI_CITY_MAP:
                    continue

                # Filter by date — skip events beyond max_days_out
                date_part = parts[1]  # e.g., "26MAR05"
                if date_part not in valid_dates:
                    continue

                weather_events.append({
                    "event_ticker": event_ticker,
                    "title": title,
                    "category": cat,
                    "mutually_exclusive": evt.get("mutually_exclusive", False),
                })

            cursor = resp.get("cursor")
            if not cursor:
                break

        logger.info("Found %d weather events within %d days", len(weather_events), max_days_out)
        return weather_events

    def _get_event_markets(self, event_ticker: str) -> List[dict]:
        """Get all individual markets for a weather event with orderbook data."""
        markets = []
        cursor = None

        while True:
            resp = self.client.get_markets(event_ticker=event_ticker, cursor=cursor)
            mkts = resp.get("markets", [])
            if not mkts:
                break

            for m in mkts:
                if m.get("status") != "active":
                    continue
                markets.append(m)

            cursor = resp.get("cursor")
            if not cursor:
                break

        return markets

    # ── NOAA Data ─────────────────────────────────────────────────────────────

    def _ensure_forecasts(self, cities: List[str]) -> None:
        """Fetch NOAA forecasts for cities we don't have yet."""
        needed = [c for c in cities if c not in self._forecasts]
        if needed:
            logger.info("Fetching NOAA forecasts for: %s", needed)
            new_forecasts = self.fetcher.fetch_all(needed)
            self._forecasts.update(new_forecasts)

    # ── Core Scan ─────────────────────────────────────────────────────────────

    def scan(
        self,
        max_days_out: int = 2,
        sigma: float = 2.5,
        min_edge: float = 0.08,
        min_volume: int = 10,
    ) -> WeatherSignal:
        """
        Full scan: find weather markets → fetch NOAA → compute edges.
        
        Args:
            max_days_out: Only consider events closing within this many days
            sigma: Forecast uncertainty in °F (std dev)
            min_edge: Minimum edge (0-1) to flag as tradeable
            min_volume: Minimum 24h volume to consider
        
        Returns:
            WeatherSignal with all found edges
        """
        signal = WeatherSignal(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Step 1: Find weather events
        events = self._find_weather_events(max_days_out)
        if not events:
            logger.warning("No weather events found within %d days", max_days_out)
            return signal

        # Step 2: Determine which cities we need forecasts for
        cities_needed = set()
        for evt in events:
            prefix = evt["event_ticker"].split("-")[0]
            city = KALSHI_CITY_MAP.get(prefix)
            if city:
                cities_needed.add(city)

        # Step 3: Fetch NOAA forecasts
        self._ensure_forecasts(list(cities_needed))

        # Step 4: Scan each event's markets for edges
        for evt in events:
            event_ticker = evt["event_ticker"]
            prefix = event_ticker.split("-")[0]
            city = KALSHI_CITY_MAP.get(prefix)

            if not city or city not in self._forecasts:
                continue

            forecast = self._forecasts[city]
            markets = self._get_event_markets(event_ticker)

            for m in markets:
                ticker = m.get("ticker", "")
                signal.total_markets_scanned += 1

                yes_bid = m.get("yes_bid", 0) or 0
                yes_ask = m.get("yes_ask", 0) or 0
                vol_24h = m.get("volume_24h", 0) or 0

                if vol_24h < min_volume:
                    continue

                edge = compute_edge(
                    forecast=forecast,
                    ticker=ticker,
                    bid=yes_bid,
                    ask=yes_ask,
                    volume_24h=vol_24h,
                    event_ticker=event_ticker,
                    sigma=sigma,
                    min_edge=min_edge,
                )

                if edge:
                    signal.edges.append(edge)

        signal.edges_found = len(signal.edges)
        # Sort by expected value descending (backtest showed EV > edge%)
        signal.edges.sort(key=lambda e: e.expected_profit_cents, reverse=True)

        self._last_signal = signal
        logger.info(
            "Weather scan complete: %d markets scanned, %d edges found",
            signal.total_markets_scanned, signal.edges_found,
        )
        return signal

    # ── Trading ───────────────────────────────────────────────────────────────

    def execute_trades(
        self,
        signal: WeatherSignal,
        max_contracts_per_trade: int = 10,
        max_total_spend_cents: int = 500,
        dry_run: bool = True,
    ) -> List[dict]:
        """
        Execute trades based on weather signal edges.
        
        Buys YES on contracts where NOAA probability significantly exceeds market price.
        
        Args:
            signal: WeatherSignal from scan()
            max_contracts_per_trade: Max contracts per order
            max_total_spend_cents: Total budget per scan cycle
            dry_run: If True, log but don't place orders
        
        Returns:
            List of order results
        """
        if not signal.edges:
            logger.info("No edges to trade")
            return []

        # Check balance
        balance_cents = 0
        try:
            bal = self.client.get_balance()
            balance_cents = bal.get("balance", 0)
            logger.info("Account balance: $%.2f", balance_cents / 100)
        except Exception as e:
            logger.error("Failed to get balance: %s", e)
            if not dry_run:
                return []

        # Limit total spend to min(configured max, available balance)
        budget = min(max_total_spend_cents, balance_cents) if not dry_run else max_total_spend_cents
        spent = 0
        orders = []

        for edge in signal.edges:
            # Only trade medium/high confidence
            if edge.confidence == "low":
                continue

            # Backtest-driven city blacklist: CHI (10% WR, -$0.09) and DC (8% WR, -$0.08)
            if edge.city in CITY_BLACKLIST:
                logger.debug("Skipping blacklisted city %s: %s", edge.city, edge.ticker)
                continue

            # Determine which side to buy and the entry price
            # edge.side == "yes" → BUY YES at market ask
            # edge.side == "no"  → BUY NO at no_ask = 100 - yes_bid
            trade_side = getattr(edge, "side", "yes")
            if trade_side == "no":
                entry_price = 100 - edge.market_bid  # NO ask derived from YES bid
                if entry_price <= 0 or entry_price >= 99:
                    continue
            else:
                entry_price = edge.market_ask
                if entry_price <= 0:
                    continue

            # Calculate order cost (entry price + 2¢ taker fee)
            cost_per_contract = entry_price + 2

            # Edge-scaled position sizing (backtest insight: bigger edges = more contracts)
            if edge.edge >= 0.30:
                size_mult = 3        # 30%+ edge: 3x base
            elif edge.edge >= 0.20:
                size_mult = 2        # 20%+ edge: 2x base
            else:
                size_mult = 1        # base size

            # Scale for top-performing cities (PHIL 58% WR, NYC 39%, LAX 40%, AUS 42%)
            if edge.city in CITY_TIER1:
                size_mult = max(size_mult, 2)  # at least 2x for tier-1 cities

            # How many can we afford?
            can_afford = (budget - spent) // cost_per_contract
            count = min(max_contracts_per_trade * size_mult, can_afford)
            if count <= 0:
                # Don't break — cheaper edges further down may still be affordable
                continue

            total_cost = count * cost_per_contract
            ev_total = count * edge.expected_profit_cents

            if dry_run:
                logger.info(
                    "[DRY RUN] BUY %s %d × %s @ %d¢  cost=$%.2f  edge=%.1f%%  EV=$%.2f  "
                    "NOAA=%.0f°F  P=%.0f%%  [%s]",
                    trade_side.upper(), count, edge.ticker, entry_price, total_cost / 100,
                    edge.edge * 100, ev_total / 100,
                    edge.noaa_forecast_f, edge.noaa_probability * 100,
                    edge.confidence,
                )
                orders.append({
                    "ticker": edge.ticker,
                    "action": "buy",
                    "side": trade_side,
                    "count": count,
                    "price": entry_price,
                    "dry_run": True,
                    "edge": edge.edge,
                    "ev_cents": ev_total,
                })
            else:
                try:
                    order_kwargs = dict(
                        ticker=edge.ticker,
                        action="buy",
                        side=trade_side,
                        count=count,
                        order_type="limit",
                    )
                    if trade_side == "no":
                        order_kwargs["no_price"] = entry_price
                    else:
                        order_kwargs["yes_price"] = entry_price
                    result = self.client.create_order(**order_kwargs)
                    logger.info(
                        "ORDER PLACED: BUY %s %d × %s @ %d¢  edge=%.1f%%  EV=$%.2f",
                        trade_side.upper(), count, edge.ticker, entry_price,
                        edge.edge * 100, ev_total / 100,
                    )
                    orders.append({
                        "ticker": edge.ticker,
                        "action": "buy",
                        "side": trade_side,
                        "count": count,
                        "price": entry_price,
                        "dry_run": False,
                        "edge": edge.edge,
                        "ev_cents": ev_total,
                        "order_result": result,
                    })
                except Exception as e:
                    logger.error("Order failed for %s: %s", edge.ticker, e)
                    orders.append({
                        "ticker": edge.ticker,
                        "error": str(e),
                    })

            spent += total_cost

        # Save signal report
        self._save_report(signal, orders)
        return orders

    # ── Reporting ─────────────────────────────────────────────────────────────

    def _save_report(self, signal: WeatherSignal, orders: List[dict]) -> None:
        """Save scan results to JSON file."""
        os.makedirs("results", exist_ok=True)
        report = {
            "timestamp": signal.timestamp,
            "markets_scanned": signal.total_markets_scanned,
            "edges_found": signal.edges_found,
            "edges": [asdict(e) for e in signal.edges],
            "orders_placed": orders,
        }
        path = "results/weather_scan.json"
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Weather scan report saved to %s", path)

    def print_scan_report(self, signal: WeatherSignal) -> None:
        """Print a formatted scan report to stdout."""
        print("\n" + "=" * 90)
        print(f"  WEATHER EDGE SCANNER — {signal.timestamp}")
        print(f"  Markets scanned: {signal.total_markets_scanned}")
        print(f"  Edges found: {signal.edges_found}")
        print("=" * 90)

        if not signal.edges:
            print("\n  No tradeable edges found.\n")
            return

        # Group by city
        by_city: Dict[str, List[MarketEdge]] = {}
        for e in signal.edges:
            by_city.setdefault(e.city, []).append(e)

        for city, edges in sorted(by_city.items()):
            print(f"\n  {city}:")
            for e in edges:
                arrow = "▲" if e.confidence == "high" else ("●" if e.confidence == "medium" else "○")
                side = getattr(e, "side", "yes").upper()
                entry = (100 - e.market_bid) if side == "NO" else e.market_ask
                print(
                    f"    {arrow} {e.ticker:<42s}  "
                    f"BUY {side}  "
                    f"NOAA={e.noaa_forecast_f:5.1f}°F  "
                    f"P={e.noaa_probability*100:5.1f}%  "
                    f"@{entry:2d}¢  "
                    f"edge={e.edge*100:+5.1f}%  "
                    f"EV={e.expected_profit_cents:+6.1f}¢  "
                    f"vol={e.market_volume_24h:5d}  "
                    f"[{e.confidence}]"
                )

        # Summary
        total_ev = sum(e.expected_profit_cents for e in signal.edges)
        high_conf = sum(1 for e in signal.edges if e.confidence == "high")
        med_conf = sum(1 for e in signal.edges if e.confidence == "medium")
        print(f"\n  Summary: {high_conf} high + {med_conf} medium confidence edges")
        print(f"  Total expected value (1 contract each): ${total_ev/100:.2f}")
        print(f"  Settle: Next day (daily weather contracts)")
        print("=" * 90 + "\n")
