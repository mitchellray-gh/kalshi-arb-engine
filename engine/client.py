"""
engine/client.py — Kalshi REST API client with RSA-PSS authentication.

Every request is signed: HMAC(timestamp_ms + METHOD + path_no_query)
using the user's RSA private key.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import Config

logger = logging.getLogger(__name__)


class KalshiAPIError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"Kalshi API {status}: {body[:200]}  ({url})")


class KalshiClient:
    """Thin, auth-aware wrapper around Kalshi's REST API."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._base = cfg.base_url
        self._key_id = cfg.api_key_id
        self._session = requests.Session()

        # Load RSA private key once
        with open(cfg.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
        logger.info("KalshiClient initialised  env=%s  base=%s", cfg.env, self._base)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """RSA-PSS signature: sign(timestamp + METHOD + path_without_query)."""
        path_clean = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_clean}".encode("utf-8")
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = self._base + path
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url += "?" + qs
        resp = self._session.get(url, headers=self._headers("GET", "/trade-api/v2" + path))
        if resp.status_code != 200:
            raise KalshiAPIError(resp.status_code, resp.text, url)
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        url = self._base + path
        resp = self._session.post(
            url, headers=self._headers("POST", "/trade-api/v2" + path),
            json=body,
        )
        if resp.status_code not in (200, 201):
            raise KalshiAPIError(resp.status_code, resp.text, url)
        return resp.json()

    def _delete(self, path: str) -> Any:
        url = self._base + path
        resp = self._session.delete(
            url, headers=self._headers("DELETE", "/trade-api/v2" + path),
        )
        if resp.status_code not in (200, 204):
            raise KalshiAPIError(resp.status_code, resp.text, url)
        return resp.json() if resp.text else {}

    # ── Market data (public — auth not required but included anyway) ──────────

    def get_events(
        self,
        status: str = "open",
        series_ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        params: dict = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/events", params)

    def get_event(self, event_ticker: str) -> dict:
        return self._get(f"/events/{event_ticker}")

    def get_markets(
        self,
        status: str = "open",
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        params: dict = {"status": status, "limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._get("/markets", params)

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook")

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        return self._get("/portfolio/balance")

    def get_positions(self) -> dict:
        return self._get("/portfolio/positions")

    def get_orders(self, status: str | None = None) -> dict:
        params = {"status": status} if status else {}
        return self._get("/portfolio/orders", params)

    def get_fills(self, ticker: str | None = None, limit: int = 100) -> dict:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/portfolio/fills", params)

    def get_settlements(self) -> dict:
        return self._get("/portfolio/settlements")

    # ── Order placement ───────────────────────────────────────────────────────

    def create_order(
        self,
        ticker: str,
        action: str,       # "buy" or "sell"
        side: str,         # "yes" or "no"
        count: int,
        order_type: str = "limit",
        yes_price: int | None = None,
        no_price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        return self._post("/portfolio/orders", body)

    def batch_create_orders(self, orders: List[dict]) -> dict:
        """Create up to 20 orders atomically."""
        return self._post("/portfolio/orders/batched", {"orders": orders})

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/portfolio/orders/{order_id}")

    def batch_cancel_orders(self, order_ids: List[str]) -> dict:
        return self._delete("/portfolio/orders/batched")

    def amend_order(self, order_id: str, price: int | None = None, count: int | None = None) -> dict:
        body: dict = {}
        if price is not None:
            body["price"] = price
        if count is not None:
            body["count"] = count
        return self._post(f"/portfolio/orders/{order_id}/amend", body)
