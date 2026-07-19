from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from usinv.data.prices.alpaca import (
    ALPACA_CORPORATE_ACTIONS_PATH,
    AlpacaHttpResponse,
    AlpacaPriceProvider,
)
from usinv.data.prices.base import (
    PriceConfigurationError,
    PricePayloadError,
    PriceQuery,
)

NOW = datetime(2026, 7, 19, 12, tzinfo=UTC)


def _body(
    *,
    symbol: str = "AAPL",
    timestamp: str = "2024-01-02T05:00:00Z",
    close: str = "101.12500001",
    token: str | None = None,
) -> bytes:
    close_value = float(close)
    payload = {
        "bars": {
            symbol: [
                {
                    "t": timestamp,
                    "o": close_value - 1,
                    "h": close_value + 1,
                    "l": close_value - 2,
                    "c": close_value,
                    "v": 1000000,
                    "n": 12345,
                    "vw": close_value - 0.5,
                }
            ]
        },
        "next_page_token": token,
    }
    return json.dumps(payload, separators=(",", ":")).encode()


class FakeTransport:
    def __init__(self, responses: list[AlpacaHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlpacaHttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        return self.responses.pop(0)


def _response(body: bytes, status: int = 200, request_id: str = "req-1") -> AlpacaHttpResponse:
    return AlpacaHttpResponse(status, {"X-Request-ID": request_id}, body)


def _provider(
    transport: FakeTransport,
    *,
    sleeps: list[float] | None = None,
    max_attempts: int = 4,
) -> AlpacaPriceProvider:
    return AlpacaPriceProvider(
        "public-key",
        "private-secret",
        transport=transport,
        now=lambda: NOW,
        sleep=(sleeps.append if sleeps is not None else lambda _seconds: None),
        max_attempts=max_attempts,
    )


def _query(*, adjustment: str = "raw", end: datetime | None = None) -> PriceQuery:
    return PriceQuery(
        ("AAPL",),
        datetime(2024, 1, 2, tzinfo=UTC),
        end or datetime(2024, 1, 4, tzinfo=UTC),
        adjustment,  # type: ignore[arg-type]
    )


def test_missing_credentials_fail_before_networking() -> None:
    with pytest.raises(PriceConfigurationError, match="credentials"):
        AlpacaPriceProvider("", "")


def test_request_bounds_must_be_aware_and_at_least_fifteen_minutes_old() -> None:
    transport = FakeTransport([])
    provider = _provider(transport)

    with pytest.raises(PriceConfigurationError, match="timezone-aware"):
        PriceQuery(
            ("AAPL",),
            datetime(2024, 1, 2),
            datetime(2024, 1, 3),
            "raw",
        )
    with pytest.raises(PriceConfigurationError, match="15 minutes"):
        provider.fetch_daily_bars(_query(end=NOW - timedelta(minutes=14, seconds=59)))

    assert not transport.calls


def test_exact_fifteen_minute_cutoff_is_allowed() -> None:
    transport = FakeTransport([_response(b'{"bars":{},"next_page_token":null}')])
    provider = _provider(transport)
    result = provider.fetch_daily_bars(_query(end=NOW - timedelta(minutes=15)))

    assert result.bars == ()
    assert len(transport.calls) == 1


def test_multi_page_request_forces_sip_raw_and_disables_vendor_symbol_mapping() -> None:
    transport = FakeTransport(
        [
            _response(_body(token="next"), request_id="page-1"),
            _response(
                _body(timestamp="2024-01-03T05:00:00Z", close="102.25"),
                request_id="page-2",
            ),
        ]
    )
    provider = _provider(transport)
    result = provider.fetch_daily_bars(_query())

    assert len(result.pages) == 2
    assert len(result.bars) == 2
    assert result.bars[0].close == Decimal("101.12500001")
    first_url, headers, _ = transport.calls[0]
    params = parse_qs(urlsplit(first_url).query, keep_blank_values=True)
    assert params["adjustment"] == ["raw"]
    assert params["feed"] == ["sip"]
    assert params["asof"] == ["-"]
    assert params["timeframe"] == ["1Day"]
    assert "private-secret" not in first_url and "public-key" not in first_url
    assert headers["APCA-API-KEY-ID"] == "public-key"
    second_params = parse_qs(urlsplit(transport.calls[1][0]).query)
    assert second_params["page_token"] == ["next"]
    assert result.pages[1].request_id == "page-2"


def test_raw_and_all_are_fetched_as_physically_separate_requests() -> None:
    transport = FakeTransport([_response(_body()), _response(_body(close="50.5625"))])
    raw, adjusted = _provider(transport).fetch_raw_and_all(
        symbols=("AAPL",),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 3, tzinfo=UTC),
    )

    assert raw.query.adjustment == "raw"
    assert adjusted.query.adjustment == "all"
    assert raw.batch_id != adjusted.batch_id
    assert parse_qs(urlsplit(transport.calls[1][0]).query)["adjustment"] == ["all"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda bar: bar.pop("vw"), "schema drifted"),
        (lambda bar: bar.update({"l": 103}), "OHLC bounds"),
        (lambda bar: bar.update({"v": 0}), "non-positive"),
    ],
)
def test_bar_schema_and_market_invariants_fail_closed(mutator: object, message: str) -> None:
    payload = json.loads(_body())
    mutator(payload["bars"]["AAPL"][0])  # type: ignore[operator]
    transport = FakeTransport([_response(json.dumps(payload).encode())])

    with pytest.raises(PricePayloadError, match=message):
        _provider(transport).fetch_daily_bars(_query())


def test_non_session_daily_bar_is_rejected() -> None:
    transport = FakeTransport([_response(_body(timestamp="2024-03-29T04:00:00Z"))])

    with pytest.raises(PricePayloadError, match="not an XNYS session"):
        _provider(transport).fetch_daily_bars(_query())


def test_bar_outside_exact_request_interval_is_rejected() -> None:
    transport = FakeTransport([_response(_body(timestamp="2024-01-05T05:00:00Z"))])

    with pytest.raises(PricePayloadError, match="outside the requested interval"):
        _provider(transport).fetch_daily_bars(_query())


def test_retriable_status_uses_retry_after_and_preserves_request_id() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        [
            AlpacaHttpResponse(429, {"Retry-After": "2", "X-Request-ID": "limited"}, b"{}"),
            _response(_body(), request_id="success"),
        ]
    )
    result = _provider(transport, sleeps=sleeps).fetch_daily_bars(_query())

    assert len(transport.calls) == 2
    assert any(delay == 2 for delay in sleeps)
    assert result.pages[0].request_id == "success"


def test_transient_network_error_is_retried_without_leaking_credentials() -> None:
    class FlakyTransport(FakeTransport):
        def get(
            self,
            url: str,
            headers: Mapping[str, str],
            timeout_seconds: float,
        ) -> AlpacaHttpResponse:
            if not self.calls:
                self.calls.append((url, dict(headers), timeout_seconds))
                raise TimeoutError("fixture timeout")
            return super().get(url, headers, timeout_seconds)

    sleeps: list[float] = []
    transport = FlakyTransport([_response(_body())])
    result = _provider(transport, sleeps=sleeps).fetch_daily_bars(_query())

    assert len(transport.calls) == 2
    assert len(result.bars) == 1
    assert any(delay == 1 for delay in sleeps)


def test_corporate_actions_probe_uses_current_endpoint_and_counts_categories() -> None:
    body = json.dumps(
        {
            "forward_splits": [{"symbol": "AAPL"}],
            "cash_dividends": [{"symbol": "AAPL"}, {"symbol": "AAPL"}],
            "partial_calls": [{"symbol": "AAPL"}],
            "next_page_token": None,
        }
    ).encode()
    transport = FakeTransport([_response(body)])
    probe = _provider(transport).probe_corporate_actions(
        symbols=("AAPL",),
        start=date(2020, 8, 1),
        end=date(2020, 9, 30),
    )

    assert probe.entitled and probe.outcome == "available"
    assert probe.category_counts["forward_splits"] == 1
    assert probe.category_counts["cash_dividends"] == 2
    assert probe.category_counts["partial_calls"] == 1
    assert urlsplit(transport.calls[0][0]).path == ALPACA_CORPORATE_ACTIONS_PATH
    assert parse_qs(urlsplit(transport.calls[0][0]).query)["region"] == ["us"]


def test_corporate_actions_forbidden_is_an_empirical_outcome_not_schema_guess() -> None:
    transport = FakeTransport([_response(b'{"message":"forbidden"}', status=403)])
    probe = _provider(transport).probe_corporate_actions(
        symbols=("AAPL",),
        start=date(2020, 8, 1),
        end=date(2020, 9, 30),
    )

    assert not probe.entitled
    assert probe.outcome == "forbidden"
    assert probe.status == 403
