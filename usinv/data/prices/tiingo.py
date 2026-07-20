"""Credential-safe Tiingo EOD spot checks for detector-flagged securities."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.config import AppConfig
from usinv.data.edgar.securities import SecurityMasterError, normalize_ticker
from usinv.data.prices.actions import CorporateActionObservation
from usinv.data.prices.base import (
    PriceConfigurationError,
    PricePayloadError,
    PriceSourcePage,
)

TIINGO_BASE_URL: Final = "https://api.tiingo.com"
TIINGO_EOD_PATH: Final = "/tiingo/daily/{ticker}/prices"
TIINGO_PROVIDER: Final = "tiingo"
MAX_FREE_REQUESTS_PER_HOUR: Final = 50
RETRIABLE_STATUS_CODES: Final = frozenset({429, 500, 502, 503, 504})
_ROW_KEYS: Final = frozenset(
    {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adjOpen",
        "adjHigh",
        "adjLow",
        "adjClose",
        "adjVolume",
        "divCash",
        "splitFactor",
    }
)


def _reject_json_constant(value: str) -> None:
    raise PricePayloadError(f"Tiingo JSON contains non-standard constant {value}")


@dataclass(frozen=True, slots=True)
class TiingoHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TiingoHttpTransport(Protocol):
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> TiingoHttpResponse:
        """Perform one GET; retry and pacing belong to the adapter."""


class UrllibTiingoTransport:
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> TiingoHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return TiingoHttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    response.read(),
                )
        except HTTPError as exc:
            return TiingoHttpResponse(
                exc.code,
                dict(exc.headers.items()) if exc.headers is not None else {},
                exc.read(),
            )


@dataclass(frozen=True, slots=True)
class TiingoDailyObservation:
    symbol: str
    timestamp: datetime
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    adjusted_volume: Decimal
    dividend_cash: Decimal
    split_factor: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise PricePayloadError("Tiingo timestamp must be timezone-aware")
        values = (
            self.open,
            self.high,
            self.low,
            self.close,
            self.adjusted_open,
            self.adjusted_high,
            self.adjusted_low,
            self.adjusted_close,
            self.split_factor,
        )
        if any(not value.is_finite() or value <= 0 for value in values):
            raise PricePayloadError("Tiingo prices and splitFactor must be positive")
        if not self.dividend_cash.is_finite() or self.dividend_cash < 0:
            raise PricePayloadError("Tiingo divCash must be finite and non-negative")
        if (
            isinstance(self.volume, bool)
            or not isinstance(self.volume, int)
            or self.volume <= 0
            or not isinstance(self.adjusted_volume, Decimal)
            or not self.adjusted_volume.is_finite()
            or self.adjusted_volume <= 0
        ):
            raise PricePayloadError("Tiingo volumes must be finite and positive")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise PricePayloadError("Tiingo raw OHLC bounds are invalid")
        if not self.low <= self.close <= self.high:
            raise PricePayloadError("Tiingo raw OHLC bounds are invalid")
        if self.adjusted_low > self.adjusted_high or not (
            self.adjusted_low <= self.adjusted_open <= self.adjusted_high
        ):
            raise PricePayloadError("Tiingo adjusted OHLC bounds are invalid")
        if not self.adjusted_low <= self.adjusted_close <= self.adjusted_high:
            raise PricePayloadError("Tiingo adjusted OHLC bounds are invalid")


@dataclass(frozen=True, slots=True)
class TiingoSpotCheck:
    symbol: str
    start: date
    end: date
    retrieved_at: datetime
    page: PriceSourcePage
    rows: tuple[TiingoDailyObservation, ...]

    @property
    def batch_id(self) -> str:
        payload = {
            "provider": TIINGO_PROVIDER,
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "retrieved_at": self.retrieved_at.astimezone(UTC).isoformat(),
            "source_sha256": self.page.content_sha256,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def action_observations(
        self,
        *,
        security_id: str,
        calendar: XNYSCalendar | None = None,
    ) -> tuple[CorporateActionObservation, ...]:
        """Use Tiingo row dates as declared ex-dates; never derive from record date."""
        session_calendar = calendar or default_calendar()
        output: list[CorporateActionObservation] = []
        for row in self.rows:
            known_at = session_calendar.session(row.session).close_at.astimezone(UTC)
            evidence = (
                f"tiingo://{self.page.content_sha256}/{self.symbol}/{row.session.isoformat()}"
            )
            if row.split_factor != Decimal(1):
                output.append(
                    CorporateActionObservation(
                        security_id,
                        row.session,
                        "split",
                        row.split_factor,
                        None,
                        TIINGO_PROVIDER,
                        known_at,
                        evidence,
                        self.batch_id,
                    )
                )
            if row.dividend_cash > 0:
                output.append(
                    CorporateActionObservation(
                        security_id,
                        row.session,
                        "cash_dividend",
                        row.dividend_cash,
                        "USD",
                        TIINGO_PROVIDER,
                        known_at,
                        evidence,
                        self.batch_id,
                    )
                )
        return tuple(output)


class TiingoSpotCheckClient:
    """Small, symbol-capped EOD client; never a universe-scale polling source."""

    def __init__(
        self,
        token: str,
        *,
        transport: TiingoHttpTransport | None = None,
        calendar: XNYSCalendar | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30,
        max_attempts: int = 4,
        max_requests_per_hour: int = MAX_FREE_REQUESTS_PER_HOUR,
    ) -> None:
        self._token = token.strip()
        if not self._token:
            raise PriceConfigurationError("Tiingo token is missing")
        if timeout_seconds <= 0 or max_attempts <= 0:
            raise PriceConfigurationError("Tiingo timeout and retry count must be positive")
        if not 1 <= max_requests_per_hour <= MAX_FREE_REQUESTS_PER_HOUR:
            raise PriceConfigurationError("Tiingo free pacing must be between 1 and 50/hour")
        self._transport = transport or UrllibTiingoTransport()
        self._calendar = calendar or default_calendar()
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._minimum_interval = 3600 / max_requests_per_hour
        self._last_request_at: float | None = None
        self._pace_lock = threading.Lock()

    @classmethod
    def from_config(cls, config: AppConfig, **kwargs: object) -> TiingoSpotCheckClient:
        variable = config.settings.credential_env.tiingo_token
        return cls(os.environ.get(variable, ""), **kwargs)

    def _clock(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            raise PriceConfigurationError("Tiingo client clock must be timezone-aware")
        return current.astimezone(UTC)

    def _pace(self) -> None:
        with self._pace_lock:
            current = self._monotonic()
            if self._last_request_at is not None:
                remaining = self._minimum_interval - (current - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
                    current = self._monotonic()
            self._last_request_at = current

    def _get(self, url: str) -> tuple[TiingoHttpResponse, datetime]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Token {self._token}",
        }
        last: TiingoHttpResponse | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            try:
                response = self._transport.get(url, headers, self._timeout_seconds)
            except OSError as exc:
                if attempt == self._max_attempts:
                    raise PricePayloadError(
                        "Tiingo request failed after transient network errors"
                    ) from exc
                self._sleep(min(2 ** (attempt - 1), 8))
                continue
            observed = self._clock()
            last = response
            if response.status not in RETRIABLE_STATUS_CODES or attempt == self._max_attempts:
                return response, observed
            self._sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError(f"unreachable Tiingo retry state: {last!r}")

    @staticmethod
    def _decimal(value: object, *, label: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise PricePayloadError(f"Tiingo {label} must be an exact JSON number")
        parsed = Decimal(value)
        if not parsed.is_finite():
            raise PricePayloadError(f"Tiingo {label} must be finite")
        return parsed

    @staticmethod
    def _integer(value: object, *, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PricePayloadError(f"Tiingo {label} must be an integer")
        return value

    def _parse_row(self, symbol: str, item: object) -> TiingoDailyObservation:
        if not isinstance(item, dict) or set(item) != _ROW_KEYS:
            raise PricePayloadError("Tiingo EOD row schema drifted")
        timestamp_value = item["date"]
        if not isinstance(timestamp_value, str):
            raise PricePayloadError("Tiingo date must be an ISO timestamp")
        try:
            timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PricePayloadError("Tiingo date is invalid") from exc
        if timestamp.tzinfo is None:
            raise PricePayloadError("Tiingo date must be timezone-aware")
        session = timestamp.date()
        try:
            self._calendar.session(session)
        except CalendarError as exc:
            raise PricePayloadError("Tiingo row is not an XNYS session") from exc
        return TiingoDailyObservation(
            symbol,
            timestamp.astimezone(UTC),
            session,
            self._decimal(item["open"], label="open"),
            self._decimal(item["high"], label="high"),
            self._decimal(item["low"], label="low"),
            self._decimal(item["close"], label="close"),
            self._integer(item["volume"], label="volume"),
            self._decimal(item["adjOpen"], label="adjOpen"),
            self._decimal(item["adjHigh"], label="adjHigh"),
            self._decimal(item["adjLow"], label="adjLow"),
            self._decimal(item["adjClose"], label="adjClose"),
            self._decimal(item["adjVolume"], label="adjVolume"),
            self._decimal(item["divCash"], label="divCash"),
            self._decimal(item["splitFactor"], label="splitFactor"),
        )

    def fetch(self, *, symbol: str, start: date, end: date) -> TiingoSpotCheck:
        try:
            normalized = normalize_ticker(symbol)
        except SecurityMasterError as exc:
            raise PriceConfigurationError("Tiingo symbol is invalid") from exc
        if start > end:
            raise PriceConfigurationError("Tiingo start cannot follow end")
        vendor_symbol = normalized.replace(".", "-")
        path = TIINGO_EOD_PATH.format(ticker=quote(vendor_symbol, safe="-"))
        query = urlencode(
            {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "format": "json",
                "resampleFreq": "daily",
                "sort": "asc",
            }
        )
        url = f"{TIINGO_BASE_URL}{path}?{query}"
        response, retrieved_at = self._get(url)
        if response.status != 200:
            raise PricePayloadError(f"Tiingo EOD request failed status={response.status}")
        try:
            payload: Any = json.loads(
                response.body,
                parse_float=Decimal,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PricePayloadError("Tiingo EOD response is not strict JSON") from exc
        if not isinstance(payload, list):
            raise PricePayloadError("Tiingo EOD response must be an array")
        rows = tuple(self._parse_row(normalized, item) for item in payload)
        if not rows:
            raise PricePayloadError("Tiingo spot check returned no EOD rows")
        if any(row.session < start or row.session > end for row in rows):
            raise PricePayloadError("Tiingo returned a row outside requested dates")
        if tuple(row.session for row in rows) != tuple(sorted({row.session for row in rows})):
            raise PricePayloadError("Tiingo rows must be unique and ascending")
        page = PriceSourcePage(
            url,
            retrieved_at,
            hashlib.sha256(response.body).hexdigest(),
            next(
                (
                    value
                    for key, value in response.headers.items()
                    if key.casefold() in {"x-request-id", "request-id"}
                ),
                None,
            ),
            response.body,
        )
        return TiingoSpotCheck(normalized, start, end, retrieved_at, page, rows)
