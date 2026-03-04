"""
engine/weather_fetcher.py — Fetch NOAA weather forecasts for Kalshi weather cities.

Uses the free NOAA Weather API (api.weather.gov) to get hourly temperature
forecasts for every city with Kalshi daily weather markets.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── City → NOAA grid mapping ─────────────────────────────────────────────────
# Each Kalshi city maps to (lat, lon) → NOAA resolves to gridpoint
# Lat/Lon taken from city centers (same reference points Kalshi uses for NWS stations)

CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "NYC":   (40.7128, -74.0060),   # New York City (Central Park)
    "BOS":   (42.3601, -71.0589),   # Boston (Logan)
    "CHI":   (41.8781, -87.6298),   # Chicago (O'Hare)
    "DEN":   (39.7392, -104.9903),  # Denver (DIA)
    "MIA":   (25.7617, -80.1918),   # Miami
    "LAX":   (33.9425, -118.4081),  # Los Angeles (LAX/USC)
    "AUS":   (30.2672, -97.7431),   # Austin
    "PHIL":  (39.9526, -75.1652),   # Philadelphia
    "HOU":   (29.7604, -95.3698),   # Houston
    "DAL":   (32.7767, -96.7970),   # Dallas
    "ATL":   (33.7490, -84.3880),   # Atlanta
    "MIN":   (44.9778, -93.2650),   # Minneapolis
    "LV":    (36.1699, -115.1398),  # Las Vegas
    "NOLA":  (29.9511, -90.0715),   # New Orleans
    "OKC":   (35.4676, -97.5164),   # Oklahoma City
    "PHX":   (33.4484, -112.0740),  # Phoenix
    "SATX":  (29.4241, -98.4936),   # San Antonio
    "SEA":   (47.6062, -122.3321),  # Seattle
    "SFO":   (37.7749, -122.4194),  # San Francisco
    "DC":    (38.9072, -77.0369),   # Washington DC
}

# Kalshi ticker prefix → city code mapping
KALSHI_CITY_MAP: Dict[str, str] = {
    "KXHIGHNY":     "NYC",  "KXLOWTNYC":    "NYC",  "KXRAINNYC":  "NYC",
    "KXHIGHTBOS":   "BOS",
    "KXHIGHCHI":    "CHI",  "KXLOWTCHI":    "CHI",
    "KXHIGHDEN":    "DEN",  "KXLOWTDEN":    "DEN",
    "KXHIGHMIA":    "MIA",  "KXLOWTMIA":    "MIA",
    "KXHIGHLAX":    "LAX",  "KXLOWTLAX":    "LAX",
    "KXHIGHAUS":    "AUS",  "KXLOWTAUS":    "AUS",
    "KXHIGHPHIL":   "PHIL", "KXLOWTPHIL":   "PHIL",
    "KXHIGHTHOU":   "HOU",
    "KXHIGHTDAL":   "DAL",
    "KXHIGHTATL":   "ATL",
    "KXHIGHTMIN":   "MIN",
    "KXHIGHTLV":    "LV",
    "KXHIGHTNOLA":  "NOLA",
    "KXHIGHTOKC":   "OKC",
    "KXHIGHTPHX":   "PHX",
    "KXHIGHTSATX":  "SATX",
    "KXHIGHTSEA":   "SEA",
    "KXHIGHTSFO":   "SFO",
    "KXHIGHTDC":    "DC",
}

# NOAA timezone offsets (UTC offset for each city for date window calculation)
CITY_UTC_OFFSET: Dict[str, int] = {
    "NYC": -5, "BOS": -5, "PHIL": -5, "DC": -5, "ATL": -5, "MIA": -5,
    "CHI": -6, "MIN": -6, "DAL": -6, "HOU": -6, "AUS": -6, "SATX": -6,
    "NOLA": -6, "OKC": -6,
    "DEN": -7, "PHX": -7,
    "LV": -8, "LAX": -8, "SFO": -8, "SEA": -8,
}


@dataclass
class CityForecast:
    """Parsed NOAA forecast for a single city."""
    city: str
    lat: float
    lon: float
    grid_id: str
    grid_x: int
    grid_y: int
    # Hourly temps in Fahrenheit, keyed by ISO timestamp
    hourly_temps_f: Dict[str, float]
    # NOAA daily max/min in Fahrenheit (from maxTemperature/minTemperature grids)
    daily_max_f: Dict[str, float]   # date string → max temp F
    daily_min_f: Dict[str, float]   # date string → min temp F
    # Precipitation probability by period
    precip_prob: Dict[str, int]     # ISO period → probability %
    fetch_time: str


def _c_to_f(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5 + 32


def _parse_iso_duration(valid_time: str) -> Tuple[str, int]:
    """Parse NOAA validTime like '2026-03-05T06:00:00+00:00/PT3H' into (start_iso, hours)."""
    parts = valid_time.split("/")
    start = parts[0]
    duration_str = parts[1] if len(parts) > 1 else "PT1H"
    hours = 1
    if "PT" in duration_str:
        h_part = duration_str.replace("PT", "").replace("H", "")
        try:
            hours = int(h_part)
        except ValueError:
            hours = 1
    return start, hours


class WeatherFetcher:
    """Fetches NOAA forecasts for all tracked cities."""

    BASE = "https://api.weather.gov"
    HEADERS = {
        "User-Agent": "(kalshi-weather-trader, weather-trading-bot)",
        "Accept": "application/geo+json",
    }

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(self.HEADERS)
        # Cache grid lookups: city → (grid_id, grid_x, grid_y)
        self._grid_cache: Dict[str, Tuple[str, int, int]] = {}

    def _get_grid(self, city: str) -> Tuple[str, int, int]:
        """Resolve city coords to NOAA grid point (cached)."""
        if city in self._grid_cache:
            return self._grid_cache[city]

        lat, lon = CITY_COORDS[city]
        url = f"{self.BASE}/points/{lat},{lon}"
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=15)
                if resp.status_code == 200:
                    props = resp.json()["properties"]
                    grid = (props["gridId"], props["gridX"], props["gridY"])
                    self._grid_cache[city] = grid
                    logger.debug("Grid for %s: %s/%s,%s", city, *grid)
                    return grid
                elif resp.status_code == 503:
                    time.sleep(2)
                    continue
                else:
                    logger.warning("NOAA /points failed for %s: %s", city, resp.status_code)
                    time.sleep(1)
            except requests.RequestException as e:
                logger.warning("NOAA request failed for %s: %s", city, e)
                time.sleep(2)
        raise RuntimeError(f"Failed to resolve NOAA grid for {city}")

    def fetch_city(self, city: str) -> CityForecast:
        """Fetch complete gridpoint data for a city from NOAA."""
        grid_id, grid_x, grid_y = self._get_grid(city)
        lat, lon = CITY_COORDS[city]

        url = f"{self.BASE}/gridpoints/{grid_id}/{grid_x},{grid_y}"
        for attempt in range(3):
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code == 200:
                    break
                time.sleep(2)
            except requests.RequestException:
                time.sleep(2)
        else:
            raise RuntimeError(f"Failed to fetch NOAA data for {city}")

        data = resp.json()["properties"]

        # Parse hourly temperatures
        hourly_temps: Dict[str, float] = {}
        for entry in data.get("temperature", {}).get("values", []):
            start, hours = _parse_iso_duration(entry["validTime"])
            val = entry["value"]
            if val is not None:
                temp_f = round(_c_to_f(val), 1)
                hourly_temps[start] = temp_f
                # Expand multi-hour periods
                if hours > 1:
                    from datetime import timedelta
                    base = datetime.fromisoformat(start)
                    for h in range(1, hours):
                        t = base + timedelta(hours=h)
                        hourly_temps[t.isoformat()] = temp_f

        # Parse daily max/min temperatures
        daily_max: Dict[str, float] = {}
        for entry in data.get("maxTemperature", {}).get("values", []):
            start, _ = _parse_iso_duration(entry["validTime"])
            val = entry["value"]
            if val is not None:
                day = start[:10]  # YYYY-MM-DD
                daily_max[day] = round(_c_to_f(val), 1)

        daily_min: Dict[str, float] = {}
        for entry in data.get("minTemperature", {}).get("values", []):
            start, _ = _parse_iso_duration(entry["validTime"])
            val = entry["value"]
            if val is not None:
                day = start[:10]
                daily_min[day] = round(_c_to_f(val), 1)

        # Parse precipitation probability
        precip_prob: Dict[str, int] = {}
        for entry in data.get("probabilityOfPrecipitation", {}).get("values", []):
            val = entry["value"]
            if val is not None:
                precip_prob[entry["validTime"]] = int(val)

        return CityForecast(
            city=city, lat=lat, lon=lon,
            grid_id=grid_id, grid_x=grid_x, grid_y=grid_y,
            hourly_temps_f=hourly_temps,
            daily_max_f=daily_max,
            daily_min_f=daily_min,
            precip_prob=precip_prob,
            fetch_time=datetime.now(timezone.utc).isoformat(),
        )

    def fetch_all(self, cities: List[str] | None = None) -> Dict[str, CityForecast]:
        """Fetch forecasts for all (or specified) cities. Returns dict city→forecast."""
        targets = cities or list(CITY_COORDS.keys())
        results: Dict[str, CityForecast] = {}

        for city in targets:
            try:
                forecast = self.fetch_city(city)
                results[city] = forecast
                logger.info("Fetched NOAA data for %s: max=%s min=%s",
                           city, forecast.daily_max_f, forecast.daily_min_f)
                time.sleep(0.3)  # Rate limit courtesy
            except Exception as e:
                logger.error("Failed to fetch %s: %s", city, e)

        return results
