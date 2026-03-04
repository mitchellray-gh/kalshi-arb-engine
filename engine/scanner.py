"""
engine/scanner.py — Scan Kalshi markets for multi-outcome event arbitrage.

PRIMARY STRATEGY: Multi-outcome ME event arb
  - BUY-ALL-YES: sum(yes_ask) + fees < 100¢ → guaranteed profit
  - BUY-ALL-NO:  sum(yes_bid) - fees > 100¢ → guaranteed profit

SECONDARY (rare): Single-market sum arb: yes_ask + no_ask < 100¢

Kalshi prices are in CENTS (1-99). Settlement = 100 cents ($1.00).
"""
from __future__ import annotations

import logging
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .client import KalshiClient
from .config import Config

logger = logging.getLogger(__name__)

# Suppress urllib3 connection pool warnings
warnings.filterwarnings("ignore", message="Connection pool is full")

FEE_PER_CONTRACT = 2   # cents per contract per side (taker fee estimate)
FEE_ROUND_TRIP   = FEE_PER_CONTRACT * 2   # buy + settle = 4 cents
_MAX_WORKERS = 10


@dataclass
class MarketQuote:
    """Current quote for a single Kalshi binary contract."""
    ticker:         str
    event_ticker:   str
    title:          str
    category:       str
    status:         str
    yes_bid:        int = 0
    yes_ask:        int = 0
    no_bid:         int = 0
    no_ask:         int = 0
    last_price:     int = 0
    volume_24h:     int = 0
    open_interest:  int = 0
    close_time:     str = ""
    result:         str = ""

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
        return self.yes_ask + self.no_ask

    @property
    def locked_profit_cents(self) -> int:
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
        return (
            self.yes_ask > 0
            and self.no_ask > 0
            and self.locked_profit_cents > 0
        )


@dataclass
class EventArbOpportunity:
    """Multi-outcome event arb opportunity detected by scanner."""
    event_ticker:      str
    event_title:       str
    arb_type:          str     # "buy_all_yes" or "buy_all_no"
    n_markets:         int
    legs:              list    # [(ticker, price_cents, bid_cents), ...]
    sum_ask:           int     # total ask cost per 1 set (cents)
    revenue:           int     # guaranteed settlement revenue (cents)
    fee_total:         int     # total fees per set (cents)
    profit_per_set:    int     # revenue - sum_ask - fees
    category:          str = ""
    collateral_type:   str = ""
    mutually_exclusive: bool = True
    # Liquidity metadata
    sum_bid:           int = 0   # sum of all leg bids
    dead_legs:         int = 0   # legs with 0 bid (unsellable)
    min_leg_bid:       int = 0   # lowest bid across all legs
    max_spread:        int = 0   # widest ask-bid spread
    exit_recovery_pct: float = 0.0  # % of cost recovered if sold instantly

    @property
    def roi_per_set(self) -> float:
        cost = self.sum_ask + self.fee_total
        return self.profit_per_set / cost if cost > 0 else 0.0

    @property
    def is_liquid(self) -> bool:
        """True if ALL legs have bids — can exit at any time."""
        return self.dead_legs == 0


@dataclass
class ScanResult:
    """Result from a full scan cycle."""
    event_arbs:     List[EventArbOpportunity]
    single_arbs:    List[MarketQuote]
    me_events:      int
    total_markets:  int
    elapsed_ms:     float


class MarketScanner:
    """Scans Kalshi for multi-outcome event arb and single-market sum arb."""

    def __init__(self, client: KalshiClient, cfg: Config) -> None:
        self._client = client
        self._cfg = cfg

    def scan(self) -> ScanResult:
        """Full scan: fetch ME events → markets → detect arbs."""
        t0 = time.perf_counter()

        # 1. Fetch all events, filter ME
        me_events = self._fetch_me_events()
        logger.info("Found %d mutually-exclusive events", len(me_events))

        # 2. Concurrently fetch markets per ME event
        event_markets = self._fetch_event_markets(me_events)
        total_mkts = sum(len(v) for v in event_markets.values())
        logger.info("Fetched %d markets across %d ME events", total_mkts, len(me_events))

        # 3. Detect multi-outcome arbs
        event_arbs = self._find_event_arbs(me_events, event_markets)

        # 4. Detect single-market arbs (rare)
        single_arbs = self._find_single_arbs(event_markets)

        elapsed = (time.perf_counter() - t0) * 1000

        if event_arbs:
            logger.info(
                "Scan: %d event arb(s), %d single arb(s) (%.0fms)",
                len(event_arbs), len(single_arbs), elapsed,
            )
            for ea in event_arbs[:5]:
                logger.info(
                    "  EVENT ARB %s  %s  %d legs  sum=%d  profit=%d¢/set",
                    ea.event_ticker, ea.arb_type, ea.n_markets, ea.sum_ask, ea.profit_per_set,
                )

        return ScanResult(
            event_arbs=event_arbs,
            single_arbs=single_arbs,
            me_events=len(me_events),
            total_markets=total_mkts,
            elapsed_ms=elapsed,
        )

    def _fetch_me_events(self) -> list[dict]:
        """Paginate events and filter mutually-exclusive."""
        all_events: list[dict] = []
        cursor = None
        while True:
            try:
                resp = self._client.get_events(status="open", cursor=cursor, limit=200)
            except Exception as exc:
                logger.error("Event fetch error: %s", exc)
                break
            batch = resp.get("events", [])
            if not batch:
                break
            all_events.extend(batch)
            cursor = resp.get("cursor")
            if not cursor:
                break
        return [e for e in all_events if e.get("mutually_exclusive")]

    def _fetch_event_markets(self, me_events: list[dict]) -> Dict[str, list[dict]]:
        """Concurrently fetch markets for each ME event."""
        result: Dict[str, list[dict]] = {}

        def _fetch_one(et: str) -> Tuple[str, list[dict]]:
            try:
                resp = self._client.get_markets(event_ticker=et, limit=200)
                return et, resp.get("markets", [])
            except Exception as exc:
                logger.debug("Failed markets for %s: %s", et, exc)
                return et, []

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = {pool.submit(_fetch_one, e["event_ticker"]): e for e in me_events}
            for fut in as_completed(futs):
                et, mkts = fut.result()
                result[et] = mkts

        return result

    def _find_event_arbs(
        self, me_events: list[dict], event_markets: Dict[str, list[dict]]
    ) -> list[EventArbOpportunity]:
        """Scan ME events for multi-outcome arb."""
        signals: list[EventArbOpportunity] = []

        for ev in me_events:
            et = ev["event_ticker"]
            raw = event_markets.get(et, [])
            if len(raw) < 2:
                continue

            parsed = [self._parse_market(m) for m in raw]
            priced = [m for m in parsed if m and m.yes_ask > 0]
            if len(priced) < 2:
                continue

            n = len(priced)
            fees = n * FEE_PER_CONTRACT

            # ── BUY ALL YES ──────────────────────────────────────────
            ya_sum = sum(m.yes_ask for m in priced)
            yb_sum = sum(m.yes_bid for m in priced)
            profit_yes = 100 - ya_sum - fees
            if profit_yes > 0:
                # Liquidity analysis
                dead = sum(1 for m in priced if m.yes_bid <= 0)
                min_bid = min(m.yes_bid for m in priced)
                max_spr = max(m.yes_ask - m.yes_bid for m in priced)
                buy_cost = ya_sum + fees
                sell_back = max(0, yb_sum - n * FEE_PER_CONTRACT)
                exit_pct = (sell_back / buy_cost * 100) if buy_cost > 0 else 0

                # LIQUIDITY GATE: skip if ANY leg has 0 bid
                if dead == 0 or not self._cfg.require_liquid_legs:
                    signals.append(EventArbOpportunity(
                        event_ticker=et,
                        event_title=ev.get("title", "")[:80],
                        arb_type="buy_all_yes",
                        n_markets=n,
                        legs=[(m.ticker, m.yes_ask, m.yes_bid) for m in priced],
                        sum_ask=ya_sum,
                        revenue=100,
                        fee_total=fees,
                        profit_per_set=profit_yes,
                        category=ev.get("category", ""),
                        collateral_type=ev.get("collateral_return_type", ""),
                        sum_bid=yb_sum,
                        dead_legs=dead,
                        min_leg_bid=min_bid,
                        max_spread=max_spr,
                        exit_recovery_pct=exit_pct,
                    ))
                else:
                    logger.debug(
                        "SKIP %s buy_all_yes: %d/%d dead legs, recovery=%.0f%%",
                        et, dead, n, exit_pct,
                    )

            # ── BUY ALL NO ────────────────────────────────────────────
            na_sum = sum((100 - m.yes_bid) if m.yes_bid > 0 else 100 for m in priced)
            nb_sum = sum((100 - m.yes_ask) if m.yes_ask > 0 else 0 for m in priced)  # no bids
            no_rev = (n - 1) * 100
            profit_no = no_rev - na_sum - fees
            if profit_no > 0:
                no_asks = [(100 - m.yes_bid) if m.yes_bid > 0 else 100 for m in priced]
                no_bids = [(100 - m.yes_ask) if m.yes_ask > 0 else 0 for m in priced]
                dead = sum(1 for b in no_bids if b <= 0)
                min_bid = min(no_bids)
                max_spr = max(a - b for a, b in zip(no_asks, no_bids))
                buy_cost = na_sum + fees
                sell_back = max(0, nb_sum - n * FEE_PER_CONTRACT)
                exit_pct = (sell_back / buy_cost * 100) if buy_cost > 0 else 0

                if dead == 0 or not self._cfg.require_liquid_legs:
                    signals.append(EventArbOpportunity(
                        event_ticker=et,
                        event_title=ev.get("title", "")[:80],
                        arb_type="buy_all_no",
                        n_markets=n,
                        legs=[(m.ticker, a, b) for m, a, b in zip(priced, no_asks, no_bids)],
                        sum_ask=na_sum,
                        revenue=no_rev,
                        fee_total=fees,
                        profit_per_set=profit_no,
                        category=ev.get("category", ""),
                        collateral_type=ev.get("collateral_return_type", ""),
                        sum_bid=nb_sum,
                        dead_legs=dead,
                        min_leg_bid=min_bid,
                        max_spread=max_spr,
                        exit_recovery_pct=exit_pct,
                    ))
                else:
                    logger.debug(
                        "SKIP %s buy_all_no: %d/%d dead legs, recovery=%.0f%%",
                        et, dead, n, exit_pct,
                    )

        # Sort: liquid arbs first, then by profit
        signals.sort(key=lambda s: (s.dead_legs, -s.profit_per_set))
        return signals

    def _find_single_arbs(self, event_markets: Dict[str, list[dict]]) -> list[MarketQuote]:
        """Single-market YES+NO sum < 100 (rare — identity is enforced)."""
        signals: list[MarketQuote] = []
        for mkts in event_markets.values():
            for raw in mkts:
                mq = self._parse_market(raw)
                if mq and mq.is_arb and mq.locked_profit_cents >= self._cfg.min_profit_cents:
                    signals.append(mq)
        signals.sort(key=lambda m: m.locked_profit_cents, reverse=True)
        return signals

    def _parse_market(self, m: dict) -> MarketQuote | None:
        ticker = m.get("ticker", "")
        if not ticker:
            return None
        yes_bid = self._price_cents(m, "yes_bid")
        yes_ask = self._price_cents(m, "yes_ask")
        no_bid  = self._price_cents(m, "no_bid")
        no_ask  = self._price_cents(m, "no_ask")
        if no_bid == 0 and yes_ask > 0:
            no_bid = 100 - yes_ask
        if no_ask == 0 and yes_bid > 0:
            no_ask = 100 - yes_bid
        return MarketQuote(
            ticker=ticker, event_ticker=m.get("event_ticker", ""),
            title=m.get("title", m.get("subtitle", "")),
            category=m.get("category", ""),
            status=m.get("status", ""),
            yes_bid=yes_bid, yes_ask=yes_ask,
            no_bid=no_bid, no_ask=no_ask,
            last_price=self._price_cents(m, "last_price"),
            volume_24h=int(m.get("volume_24h", 0)),
            open_interest=int(m.get("open_interest", 0)),
            close_time=m.get("close_time", ""),
            result=m.get("result", ""),
        )

    @staticmethod
    def _price_cents(m: dict, key: str) -> int:
        val = m.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
        dval = m.get(key + "_dollars")
        if dval:
            try:
                return int(round(float(dval) * 100))
            except (ValueError, TypeError):
                pass
        return 0
