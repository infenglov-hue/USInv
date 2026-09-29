"""Alpaca market-data client: daily bars, corporate actions, assets, snapshots.

Free ("Basic") Alpaca accounts get consolidated SIP history back to 2016,
including symbols that have since delisted, as long as the request does not
touch the most recent 15 minutes.  Live snapshots use the free IEX feed.

Credentials: ``ALPACA_KEY_ID`` / ``ALPACA_SECRET_KEY`` (``APCA_API_*`` aliases
accepted).  They are only sent to alpaca.markets.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import pandas as pd
import requests

from us_picker.utils.rate_limiter import RateLimiter

logger = logging.getLogger("us_picker.data.sources.alpaca")

_DATA_URL = "https://data.alpaca.markets"
_DEFAULT_TRADING_URL = "https://paper-api.alpaca.markets"
_LIVE_TRADING_URL = "https://api.alpaca.markets"
_NY = "America/New_York"
_SYMBOLS_PER_REQUEST = 100


def _credential(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise RuntimeError(f"Missing Alpaca credential: set {names[0]}")


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class AlpacaClient:
    """Minimal REST client over Alpaca's market-data and assets endpoints."""

    def __init__(
        self,
        rate_limiter: Optional[RateLimiter] = None,
        session: Optional[requests.Session] = None,
        feed: str = "sip",
        max_retries: int = 5,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter(min_delay=0.31, name="alpaca")
        self._session = session or requests.Session()
        self._feed = feed
        self._max_retries = max_retries
        self._headers = {
            "APCA-API-KEY-ID": _credential("ALPACA_KEY_ID", "APCA_API_KEY_ID"),
            "APCA-API-SECRET-KEY": _credential("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY"),
        }
        self._trading_url = (
            os.environ.get("APCA_API_BASE_URL", "").strip().rstrip("/") or _DEFAULT_TRADING_URL
        )
        if self._trading_url.endswith("/v2"):
            self._trading_url = self._trading_url[:-3]

    # -- transport ---------------------------------------------------------

    def _get(self, url: str, params: Optional[dict] = None) -> dict | list:
        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.wait()
            try:
                response = self._session.get(url, headers=self._headers, params=params, timeout=60)
            except requests.RequestException as exc:
                logger.warning("Alpaca request failed (%s): %s", attempt, exc)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning("Alpaca %s (attempt %s)", response.status_code, attempt)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Alpaca request kept failing: {url}")

    # -- bars --------------------------------------------------------------

    def fetch_daily_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjustment: str = "raw",
    ) -> pd.DataFrame:
        """Daily bars for many symbols.

        Returns columns ``symbol, date, open, high, low, close, volume``;
        ``date`` is the New York session date.  ``end`` is clamped so the
        request never touches the free plan's 15-minute SIP embargo.
        """

        if not symbols:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        end_ts = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        embargo = datetime.now(timezone.utc) - timedelta(minutes=16)
        end_ts = min(end_ts, embargo)
        records: list[dict] = []
        for chunk in _chunks(sorted(set(symbols)), _SYMBOLS_PER_REQUEST):
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10000,
                "adjustment": adjustment,
                "feed": self._feed,
            }
            while True:
                payload = self._get(f"{_DATA_URL}/v2/stocks/bars", params)
                for symbol, bars in (payload.get("bars") or {}).items():
                    for bar in bars:
                        records.append(
                            {
                                "symbol": symbol,
                                "t": bar["t"],
                                "open": bar.get("o"),
                                "high": bar.get("h"),
                                "low": bar.get("l"),
                                "close": bar.get("c"),
                                "volume": bar.get("v"),
                            }
                        )
                token = payload.get("next_page_token")
                if not token:
                    break
                params = dict(params, page_token=token)
        df = pd.DataFrame.from_records(records)
        if df.empty:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df.pop("t"), utc=True).dt.tz_convert(_NY).dt.date
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def fetch_price_history(
        self, symbols: list[str], start: date, end: date
    ) -> pd.DataFrame:
        """Raw OHLCV plus total-return ``adjusted_close`` (splits + dividends)."""

        raw = self.fetch_daily_bars(symbols, start, end, adjustment="raw")
        if raw.empty:
            return raw
        adjusted = self.fetch_daily_bars(symbols, start, end, adjustment="all")
        adjusted = adjusted[["symbol", "date", "close"]].rename(columns={"close": "adjusted_close"})
        merged = raw.merge(adjusted, on=["symbol", "date"], how="left")
        merged["source"] = "ALPACA"
        return merged

    # -- corporate actions -------------------------------------------------

    def fetch_corporate_actions(
        self, symbols: list[str], start: date, end: date
    ) -> pd.DataFrame:
        """Splits and cash dividends as ``symbol, action_date, action_type, ...``.

        ``adjustment_factor`` for splits is new/old shares (4.0 for 4:1,
        0.1 for a 1:10 reverse split); for dividends it is the cash rate.
        """

        rows: list[dict] = []
        for chunk in _chunks(sorted(set(symbols)), _SYMBOLS_PER_REQUEST):
            # The endpoint caps the window; walk it a year at a time.
            window_start = start
            while window_start <= end:
                window_end = min(end, window_start + timedelta(days=364))
                params = {
                    "symbols": ",".join(chunk),
                    "types": "forward_split,reverse_split,cash_dividend",
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                    "limit": 1000,
                }
                while True:
                    payload = self._get(f"{_DATA_URL}/v1/corporate-actions", params)
                    actions = payload.get("corporate_actions") or {}
                    for kind in ("forward_splits", "reverse_splits"):
                        for item in actions.get(kind) or []:
                            old, new = item.get("old_rate"), item.get("new_rate")
                            if not old or not new or not item.get("ex_date"):
                                continue
                            rows.append(
                                {
                                    "symbol": item["symbol"],
                                    "action_date": date.fromisoformat(item["ex_date"]),
                                    "action_type": "SPLIT",
                                    "adjustment_factor": float(new) / float(old),
                                    "details": {"old_rate": old, "new_rate": new, "kind": kind},
                                }
                            )
                    for item in actions.get("cash_dividends") or []:
                        if not item.get("ex_date") or item.get("rate") is None:
                            continue
                        rows.append(
                            {
                                "symbol": item["symbol"],
                                "action_date": date.fromisoformat(item["ex_date"]),
                                "action_type": "DIVIDEND",
                                "adjustment_factor": float(item["rate"]),
                                "details": {
                                    "rate": item["rate"],
                                    "special": bool(item.get("special")),
                                    "foreign": bool(item.get("foreign")),
                                    "payable_date": item.get("payable_date"),
                                },
                            }
                        )
                    token = payload.get("next_page_token")
                    if not token:
                        break
                    params = dict(params, page_token=token)
                window_start = window_end + timedelta(days=1)
        return pd.DataFrame.from_records(
            rows, columns=["symbol", "action_date", "action_type", "adjustment_factor", "details"]
        )

    # -- reference / live --------------------------------------------------

    def fetch_assets(self, status: str = "active") -> list[dict]:
        """US equity assets from the trading API (symbol, name, exchange, ...)."""

        params = {"status": status, "asset_class": "us_equity"}
        # Paper and live keys authenticate against different trading hosts;
        # market data accepts either. Try the configured host, then the other.
        hosts = [self._trading_url]
        for other in (_DEFAULT_TRADING_URL, _LIVE_TRADING_URL):
            if other not in hosts:
                hosts.append(other)
        last_error: Optional[Exception] = None
        for host in hosts:
            try:
                payload = self._get(f"{host}/v2/assets", params)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (401, 403):
                    last_error = exc
                    continue
                raise
            return list(payload) if isinstance(payload, list) else []
        raise RuntimeError(f"Alpaca rejected the credentials on every trading host: {last_error}")

    def fetch_snapshots(self, symbols: list[str], feed: str = "iex") -> dict[str, dict]:
        result: dict[str, dict] = {}
        for chunk in _chunks(sorted(set(symbols)), _SYMBOLS_PER_REQUEST):
            payload = self._get(
                f"{_DATA_URL}/v2/stocks/snapshots",
                {"symbols": ",".join(chunk), "feed": feed},
            )
            if isinstance(payload, dict):
                result.update({k: v for k, v in payload.items() if isinstance(v, dict)})
        return result
