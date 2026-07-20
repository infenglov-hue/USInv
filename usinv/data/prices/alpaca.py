"""Fail-closed Alpaca Market Data adapter for delayed consolidated daily bars."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as daytime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from usinv.calendar import EXCHANGE_TIMEZONE, CalendarError, XNYSCalendar, default_calendar
from usinv.config import AppConfig
from usinv.data.edgar.securities import SecurityMasterError, normalize_ticker
from usinv.data.prices.actions import CorporateActionObservation
from usinv.data.prices.base import (
    PriceConfigurationError,
    PriceFetchResult,
    PriceMappingError,
    PricePayloadError,
    PriceProvider,
    PriceQuery,
    PriceSecurityBinding,
    PriceSourcePage,
    VendorBarIssue,
    VendorDailyBar,
)

ALPACA_DATA_BASE_URL: Final = "https://data.alpaca.markets"
ALPACA_BARS_PATH: Final = "/v2/stocks/bars"
ALPACA_CORPORATE_ACTIONS_PATH: Final = "/v1/corporate-actions"
ALPACA_PROVIDER: Final = "alpaca"
ALPACA_BAR_DEFINITION: Final = "alpaca-v2-stocks-bars-1day-sip-trade-aggregate"
LATEST_RESTRICTION: Final = timedelta(minutes=15)
MAX_BASIC_HISTORICAL_REQUESTS_PER_MINUTE: Final = 200
RETRIABLE_STATUS_CODES: Final = frozenset({429, 500, 502, 503, 504})
_BAR_KEYS: Final = frozenset({"t", "o", "h", "l", "c", "v", "n", "vw"})
_BARS_TOP_LEVEL_KEYS: Final = frozenset({"bars", "next_page_token", "currency"})
_ACTION_CATEGORIES: Final = frozenset(
    {
        "cash_dividends",
        "stock_dividends",
        "forward_splits",
        "reverse_splits",
        "unit_splits",
        "cash_mergers",
        "stock_mergers",
        "stock_and_cash_mergers",
        "redemptions",
        "name_changes",
        "worthless_removals",
        "spin_offs",
        "rights_distributions",
        "partial_calls",
        "reorganizations",
    }
)


class _QuarantinableBarError(PricePayloadError):
    def __init__(self, session: date, detail: str) -> None:
        super().__init__(detail)
        self.session = session


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == expected), None)


class AlpacaHttpError(PricePayloadError):
    """Raised after a permanent response or exhausted retry budget."""

    def __init__(
        self,
        status: int | None,
        url: str,
        request_id: str | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class AlpacaHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class AlpacaHttpTransport(Protocol):
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlpacaHttpResponse:
        """Perform one HTTP GET without retries or policy decisions."""


class UrllibAlpacaTransport:
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlpacaHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return AlpacaHttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    response.read(),
                )
        except HTTPError as exc:
            return AlpacaHttpResponse(
                exc.code,
                dict(exc.headers.items()) if exc.headers is not None else {},
                exc.read(),
            )


CorporateActionsOutcome = Literal[
    "available",
    "authentication_failed",
    "forbidden",
    "not_found",
]


@dataclass(frozen=True, slots=True)
class CorporateActionsProbe:
    """Empirical endpoint/entitlement result; semantic ingestion belongs to Phase 2.2."""

    outcome: CorporateActionsOutcome
    status: int
    category_counts: Mapping[str, int]
    pages: tuple[PriceSourcePage, ...]

    @property
    def entitled(self) -> bool:
        return self.outcome == "available"


@dataclass(frozen=True, slots=True)
class CorporateActionsFetchResult:
    """Mapped split/dividend evidence; other action semantics remain later work."""

    start: date
    end: date
    pages: tuple[PriceSourcePage, ...]
    observations: tuple[CorporateActionObservation, ...]

    @property
    def batch_id(self) -> str:
        payload = {
            "provider": ALPACA_PROVIDER,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "pages": [
                {
                    "url": page.url,
                    "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
                    "sha256": page.content_sha256,
                    "request_id": page.request_id,
                }
                for page in self.pages
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class AlpacaPriceProvider(PriceProvider):
    """Authenticated adapter with Basic-tier timing, rate and schema guardrails."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        transport: AlpacaHttpTransport | None = None,
        calendar: XNYSCalendar | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30,
        max_attempts: int = 4,
        max_requests_per_minute: int = MAX_BASIC_HISTORICAL_REQUESTS_PER_MINUTE,
    ) -> None:
        self._key_id = key_id.strip()
        self._secret_key = secret_key.strip()
        if not self._key_id or not self._secret_key:
            raise PriceConfigurationError("Alpaca credentials are missing")
        if timeout_seconds <= 0 or max_attempts <= 0:
            raise PriceConfigurationError("Alpaca timeout and retry count must be positive")
        if not 1 <= max_requests_per_minute <= MAX_BASIC_HISTORICAL_REQUESTS_PER_MINUTE:
            raise PriceConfigurationError(
                "Alpaca Basic pacing must be between 1 and 200 requests/min"
            )
        self._transport = transport or UrllibAlpacaTransport()
        self._calendar = calendar or default_calendar()
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._minimum_interval = 60 / max_requests_per_minute
        self._last_request_at: float | None = None
        self._pace_lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        **kwargs: object,
    ) -> AlpacaPriceProvider:
        credentials = config.settings.credential_env
        return cls(
            os.environ.get(credentials.alpaca_key_id, ""),
            os.environ.get(credentials.alpaca_secret_key, ""),
            **kwargs,
        )

    @staticmethod
    def _json(body: bytes, *, context: str) -> Mapping[str, Any]:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant: {value}")

        try:
            payload = json.loads(
                body,
                parse_float=Decimal,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PricePayloadError(f"Alpaca {context} response is not strict JSON") from exc
        if not isinstance(payload, dict):
            raise PricePayloadError(f"Alpaca {context} response must be an object")
        return payload

    def _pace(self) -> None:
        with self._pace_lock:
            current = self._monotonic()
            if self._last_request_at is not None:
                remaining = self._minimum_interval - (current - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
                    current = self._monotonic()
            self._last_request_at = current

    def _clock(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            raise PriceConfigurationError("Alpaca client clock must be timezone-aware")
        return current.astimezone(UTC)

    def _url(self, path: str, params: Mapping[str, str | int]) -> str:
        if path not in {ALPACA_BARS_PATH, ALPACA_CORPORATE_ACTIONS_PATH}:
            raise PriceConfigurationError("unsupported Alpaca endpoint")
        return f"{ALPACA_DATA_BASE_URL}{path}?{urlencode(params)}"

    def _get(self, url: str) -> tuple[AlpacaHttpResponse, datetime]:
        headers = {
            "Accept": "application/json",
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret_key,
        }
        last_response: AlpacaHttpResponse | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            try:
                response = self._transport.get(url, headers, self._timeout_seconds)
            except OSError as exc:
                if attempt == self._max_attempts:
                    raise AlpacaHttpError(
                        None,
                        url,
                        None,
                        "Alpaca request failed after transient network errors",
                    ) from exc
                self._sleep(min(2 ** (attempt - 1), 8))
                continue
            observed_at = self._clock()
            last_response = response
            if response.status not in RETRIABLE_STATUS_CODES or attempt == self._max_attempts:
                return response, observed_at
            retry_after = _header(response.headers, "Retry-After")
            try:
                delay = (
                    float(retry_after) if retry_after is not None else min(2 ** (attempt - 1), 8)
                )
            except ValueError:
                delay = min(2 ** (attempt - 1), 8)
            self._sleep(max(delay, 0))
        raise AssertionError(f"unreachable Alpaca retry state: {last_response!r}")

    @staticmethod
    def _source_page(
        url: str,
        response: AlpacaHttpResponse,
        observed_at: datetime,
    ) -> PriceSourcePage:
        return PriceSourcePage(
            url=url,
            retrieved_at=observed_at,
            content_sha256=hashlib.sha256(response.body).hexdigest(),
            request_id=_header(response.headers, "X-Request-ID"),
            body=response.body,
        )

    @staticmethod
    def _decimal(value: object, name: str) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise PricePayloadError(f"Alpaca bar {name} must be an exact JSON number")
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise PricePayloadError(f"Alpaca bar {name} is invalid") from exc
        if not parsed.is_finite():
            raise PricePayloadError(f"Alpaca bar {name} must be finite")
        return parsed

    @staticmethod
    def _integer(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise PricePayloadError(f"Alpaca bar {name} must be an integer")
        return value

    def _parse_bar(
        self,
        symbol: str,
        value: object,
        *,
        page_index: int,
    ) -> VendorDailyBar:
        if not isinstance(value, dict) or set(value) != _BAR_KEYS:
            raise PricePayloadError("Alpaca daily-bar schema drifted")
        timestamp_value = value["t"]
        if not isinstance(timestamp_value, str):
            raise PricePayloadError("Alpaca bar timestamp must be an ISO string")
        try:
            timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PricePayloadError("Alpaca bar timestamp is invalid") from exc
        if timestamp.tzinfo is None:
            raise PricePayloadError("Alpaca bar timestamp must be timezone-aware")
        exchange_timestamp = timestamp.astimezone(EXCHANGE_TIMEZONE)
        if exchange_timestamp.timetz().replace(tzinfo=None) != daytime.min:
            raise PricePayloadError("Alpaca daily bar must be timestamped at New York midnight")
        session = exchange_timestamp.date()
        try:
            self._calendar.session(session)
        except CalendarError as exc:
            raise PricePayloadError("Alpaca daily bar is not an XNYS session") from exc
        open_value = self._decimal(value["o"], "open")
        high = self._decimal(value["h"], "high")
        low = self._decimal(value["l"], "low")
        close = self._decimal(value["c"], "close")
        vwap = self._decimal(value["vw"], "vwap")
        volume = self._integer(value["v"], "volume")
        trade_count = self._integer(value["n"], "trade_count")
        if min(open_value, high, low, close, vwap) <= 0 or volume <= 0 or trade_count < 0:
            raise _QuarantinableBarError(
                session,
                "Alpaca daily bar contains non-positive price/volume",
            )
        if low > high or not low <= open_value <= high or not low <= close <= high:
            raise PricePayloadError("Alpaca daily bar violates OHLC bounds")
        return VendorDailyBar(
            normalize_ticker(symbol),
            timestamp.astimezone(UTC),
            session,
            open_value,
            high,
            low,
            close,
            volume,
            trade_count,
            vwap,
            page_index,
        )

    def _validate_query(self, query: PriceQuery) -> None:
        cutoff = self._clock() - LATEST_RESTRICTION
        if query.end.astimezone(UTC) > cutoff:
            raise PriceConfigurationError(
                "Alpaca Basic request end must be at least 15 minutes behind now"
            )

    def fetch_daily_bars(self, query: PriceQuery) -> PriceFetchResult:
        """Fetch all multi-symbol daily pages with SIP and symbol remapping disabled."""
        self._validate_query(query)
        pages: list[PriceSourcePage] = []
        bars: list[VendorDailyBar] = []
        provider_issues: list[VendorBarIssue] = []
        quarantined_symbols: set[str] = set()
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str | int] = {
                "symbols": ",".join(query.symbols),
                "timeframe": query.timeframe,
                "start": query.start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "end": query.end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "adjustment": query.adjustment,
                "feed": query.feed,
                "asof": "-",
                "sort": "asc",
                "limit": 10000,
            }
            if page_token is not None:
                params["page_token"] = page_token
            url = self._url(ALPACA_BARS_PATH, params)
            response, observed_at = self._get(url)
            request_id = _header(response.headers, "X-Request-ID")
            if response.status != 200:
                raise AlpacaHttpError(
                    response.status,
                    url,
                    request_id,
                    f"Alpaca bars request failed status={response.status} request_id={request_id}",
                )
            page_index = len(pages)
            pages.append(self._source_page(url, response, observed_at))
            payload = self._json(response.body, context="bars")
            if not set(payload).issubset(_BARS_TOP_LEVEL_KEYS) or "bars" not in payload:
                raise PricePayloadError("Alpaca bars top-level schema drifted")
            symbol_bars = payload["bars"]
            if not isinstance(symbol_bars, dict):
                raise PricePayloadError("Alpaca bars field must map symbols to arrays")
            for symbol, values in symbol_bars.items():
                try:
                    normalized_symbol = (
                        normalize_ticker(symbol) if isinstance(symbol, str) else None
                    )
                except SecurityMasterError as exc:
                    raise PricePayloadError("Alpaca returned an invalid symbol") from exc
                if normalized_symbol not in query.symbols:
                    raise PricePayloadError("Alpaca returned an unrequested symbol")
                if not isinstance(values, list):
                    raise PricePayloadError("Alpaca symbol bars must be an array")
                parsed_bars: list[VendorDailyBar] = []
                for item in values:
                    try:
                        parsed_bars.append(self._parse_bar(symbol, item, page_index=page_index))
                    except _QuarantinableBarError as exc:
                        quarantined_symbols.add(normalized_symbol)
                        provider_issues.append(
                            VendorBarIssue(
                                normalized_symbol,
                                exc.session,
                                "invalid_provider_bar",
                                str(exc),
                                page_index,
                            )
                        )
                if any(
                    bar.timestamp < query.start.astimezone(UTC)
                    or bar.timestamp > query.end.astimezone(UTC)
                    for bar in parsed_bars
                ):
                    raise PricePayloadError("Alpaca returned a bar outside the requested interval")
                bars.extend(parsed_bars)
            next_token = payload.get("next_page_token")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise PricePayloadError("Alpaca pagination token is invalid or cyclic")
            seen_tokens.add(next_token)
            page_token = next_token
        bars = [bar for bar in bars if bar.vendor_symbol not in quarantined_symbols]
        bars.sort(key=lambda item: (item.vendor_symbol, item.session, item.timestamp))
        return PriceFetchResult(
            ALPACA_PROVIDER,
            ALPACA_BAR_DEFINITION,
            query,
            tuple(pages),
            tuple(bars),
            tuple(provider_issues),
        )

    def fetch_raw_and_all(
        self,
        *,
        symbols: tuple[str, ...],
        start: datetime,
        end: datetime,
    ) -> tuple[PriceFetchResult, PriceFetchResult]:
        """Fetch physically separate raw and all-adjusted evidence batches."""
        raw = self.fetch_daily_bars(PriceQuery(symbols, start, end, "raw"))
        adjusted = self.fetch_daily_bars(PriceQuery(symbols, start, end, "all"))
        return raw, adjusted

    @staticmethod
    def _action_binding(
        symbol: str,
        session: date,
        bindings: tuple[PriceSecurityBinding, ...],
    ) -> PriceSecurityBinding:
        matches = tuple(
            binding
            for binding in bindings
            if binding.ticker == symbol and binding.contains(session)
        )
        if len(matches) != 1:
            raise PriceMappingError(
                "Alpaca action requires exactly one security/exchange/date binding"
            )
        return matches[0]

    def _parse_declared_action(
        self,
        *,
        category: str,
        item: object,
        page: PriceSourcePage,
        batch_id: str,
        bindings: tuple[PriceSecurityBinding, ...],
    ) -> CorporateActionObservation:
        allowed_by_category = {
            "forward_splits": {
                "id",
                "symbol",
                "cusip",
                "new_rate",
                "old_rate",
                "process_date",
                "ex_date",
                "record_date",
                "payable_date",
                "due_bill_redemption_date",
            },
            "reverse_splits": {
                "id",
                "symbol",
                "old_cusip",
                "new_cusip",
                "new_rate",
                "old_rate",
                "process_date",
                "ex_date",
                "record_date",
                "payable_date",
            },
            "cash_dividends": {
                "id",
                "symbol",
                "cusip",
                "rate",
                "special",
                "foreign",
                "process_date",
                "ex_date",
                "record_date",
                "payable_date",
                "due_bill_on_date",
                "due_bill_off_date",
            },
        }
        required_by_category = {
            "forward_splits": {"id", "symbol", "new_rate", "old_rate", "ex_date"},
            "reverse_splits": {"id", "symbol", "new_rate", "old_rate", "ex_date"},
            "cash_dividends": {"id", "symbol", "rate", "ex_date"},
        }
        if not isinstance(item, dict):
            raise PricePayloadError("Alpaca declared action must be an object")
        keys = set(item)
        if not required_by_category[category] <= keys or not keys <= allowed_by_category[category]:
            raise PricePayloadError("Alpaca declared-action schema drifted")
        symbol_value = item["symbol"]
        action_id = item["id"]
        ex_date_value = item["ex_date"]
        if not isinstance(symbol_value, str) or not isinstance(action_id, str) or not action_id:
            raise PricePayloadError("Alpaca declared-action identity is invalid")
        try:
            symbol = normalize_ticker(symbol_value)
            effective_session = date.fromisoformat(ex_date_value)
            known_at = self._calendar.session(effective_session).close_at.astimezone(UTC)
        except (SecurityMasterError, TypeError, ValueError, CalendarError) as exc:
            raise PricePayloadError("Alpaca declared-action symbol/ex-date is invalid") from exc
        binding = self._action_binding(symbol, effective_session, bindings)
        if category == "cash_dividends":
            value = self._decimal(item["rate"], "cash dividend rate")
            action_type = "cash_dividend"
            currency = "USD"
        else:
            new_rate = self._decimal(item["new_rate"], "split new_rate")
            old_rate = self._decimal(item["old_rate"], "split old_rate")
            if old_rate <= 0:
                raise PricePayloadError("Alpaca split old_rate must be positive")
            value = new_rate / old_rate
            action_type = "split"
            currency = None
        return CorporateActionObservation(
            binding.security_id,
            effective_session,
            action_type,  # type: ignore[arg-type]
            value,
            currency,
            ALPACA_PROVIDER,
            known_at,
            f"alpaca://{page.content_sha256}/{category}/{action_id}",
            batch_id,
        )

    def fetch_corporate_actions(
        self,
        *,
        bindings: tuple[PriceSecurityBinding, ...],
        start: date,
        end: date,
    ) -> CorporateActionsFetchResult:
        """Fetch and date-map declared splits/cash dividends for reconciliation."""
        if not bindings or start > end:
            raise PriceConfigurationError("Alpaca action bindings/bounds are invalid")
        symbols = tuple(sorted({binding.ticker for binding in bindings}))
        pages: list[PriceSourcePage] = []
        payload_pages: list[tuple[Mapping[str, Any], PriceSourcePage]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str | int] = {
                "symbols": ",".join(symbols),
                "types": "forward_split,reverse_split,cash_dividend",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sort": "asc",
                "region": "us",
                "limit": 1000,
            }
            if token is not None:
                params["page_token"] = token
            url = self._url(ALPACA_CORPORATE_ACTIONS_PATH, params)
            response, observed_at = self._get(url)
            request_id = _header(response.headers, "X-Request-ID")
            if response.status != 200:
                raise AlpacaHttpError(
                    response.status,
                    url,
                    request_id,
                    "Alpaca declared-actions request failed "
                    f"status={response.status} request_id={request_id}",
                )
            page = self._source_page(url, response, observed_at)
            pages.append(page)
            payload = self._json(response.body, context="declared-actions")
            if not set(payload).issubset({"corporate_actions", "next_page_token"}):
                raise PricePayloadError("Alpaca declared-actions top-level schema drifted")
            envelope = payload.get("corporate_actions")
            if not isinstance(envelope, dict) or not set(envelope).issubset(
                {"forward_splits", "reverse_splits", "cash_dividends"}
            ):
                raise PricePayloadError("Alpaca declared-actions envelope drifted")
            if any(not isinstance(items, list) for items in envelope.values()):
                raise PricePayloadError("Alpaca declared-action category must be an array")
            payload_pages.append((payload, page))
            next_token = payload.get("next_page_token")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise PricePayloadError("Alpaca declared-action pagination token is invalid")
            seen_tokens.add(next_token)
            token = next_token
        base = CorporateActionsFetchResult(start, end, tuple(pages), ())
        observations: list[CorporateActionObservation] = []
        for payload, page in payload_pages:
            envelope = payload["corporate_actions"]
            assert isinstance(envelope, dict)
            for category, items in sorted(envelope.items()):
                assert isinstance(items, list)
                observations.extend(
                    self._parse_declared_action(
                        category=category,
                        item=item,
                        page=page,
                        batch_id=base.batch_id,
                        bindings=bindings,
                    )
                    for item in items
                )
        observations.sort(
            key=lambda item: (
                item.security_id,
                item.effective_session,
                item.action_type,
                item.evidence_pointer,
            )
        )
        return CorporateActionsFetchResult(start, end, tuple(pages), tuple(observations))

    def probe_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...],
        start: date,
        end: date,
    ) -> CorporateActionsProbe:
        """Measure current endpoint access without yet normalizing action semantics."""
        try:
            normalized = tuple(sorted({normalize_ticker(symbol) for symbol in symbols}))
        except SecurityMasterError as exc:
            raise PriceConfigurationError(
                "corporate-action probe contains an invalid symbol"
            ) from exc
        if not normalized or start > end:
            raise PriceConfigurationError("corporate-action probe bounds are invalid")
        pages: list[PriceSourcePage] = []
        counts = dict.fromkeys(sorted(_ACTION_CATEGORIES), 0)
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, str | int] = {
                "symbols": ",".join(normalized),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sort": "asc",
                "region": "us",
                "limit": 1000,
            }
            if token is not None:
                params["page_token"] = token
            url = self._url(ALPACA_CORPORATE_ACTIONS_PATH, params)
            response, observed_at = self._get(url)
            page = self._source_page(url, response, observed_at)
            if response.status != 200:
                outcome_by_status: dict[int, CorporateActionsOutcome] = {
                    401: "authentication_failed",
                    403: "forbidden",
                    404: "not_found",
                }
                outcome = outcome_by_status.get(response.status)
                if outcome is None:
                    request_id = _header(response.headers, "X-Request-ID")
                    raise AlpacaHttpError(
                        response.status,
                        url,
                        request_id,
                        "Alpaca corporate-actions probe failed "
                        f"status={response.status} request_id={request_id}",
                    )
                return CorporateActionsProbe(outcome, response.status, counts, (page,))
            pages.append(page)
            payload = self._json(response.body, context="corporate-actions")
            if not set(payload).issubset({"corporate_actions", "next_page_token"}):
                raise PricePayloadError("Alpaca corporate-actions top-level schema drifted")
            actions = payload.get("corporate_actions")
            if not isinstance(actions, dict):
                raise PricePayloadError("Alpaca corporate_actions envelope must be an object")
            if not set(actions).issubset(_ACTION_CATEGORIES):
                raise PricePayloadError("Alpaca corporate-action categories drifted")
            for category, value in actions.items():
                if not isinstance(value, list):
                    raise PricePayloadError("Alpaca corporate-action category must be an array")
                counts[category] += len(value)
            next_token = payload.get("next_page_token")
            if next_token is None:
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise PricePayloadError("Alpaca corporate-action pagination token is invalid")
            seen_tokens.add(next_token)
            token = next_token
        return CorporateActionsProbe("available", 200, counts, tuple(pages))
