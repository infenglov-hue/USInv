from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from usinv.data.prices.base import PricePayloadError
from usinv.data.prices.tiingo import (
    TiingoHttpResponse,
    TiingoSpotCheckClient,
)

NOW = datetime(2026, 7, 20, 7, tzinfo=UTC)


def _row(
    *,
    day: str = "2024-01-04T00:00:00.000Z",
    dividend: float = 1.0,
    split: float = 2.0,
) -> dict[str, object]:
    return {
        "date": day,
        "open": 50.0,
        "high": 52.0,
        "low": 49.0,
        "close": 51.0,
        "volume": 1000,
        "adjOpen": 25.0,
        "adjHigh": 26.0,
        "adjLow": 24.5,
        "adjClose": 25.5,
        "adjVolume": 2000,
        "divCash": dividend,
        "splitFactor": split,
    }


class FakeTransport:
    def __init__(self, responses: list[TiingoHttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> TiingoHttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        return self.responses.pop(0)


def _response(payload: object, *, status: int = 200) -> TiingoHttpResponse:
    return TiingoHttpResponse(
        status,
        {"X-Request-ID": "tiingo-request"},
        json.dumps(payload, separators=(",", ":")).encode(),
    )


def _client(
    transport: FakeTransport, *, sleeps: list[float] | None = None
) -> TiingoSpotCheckClient:
    return TiingoSpotCheckClient(
        "private-token",
        transport=transport,
        now=lambda: NOW,
        sleep=(sleeps if sleeps is not None else []).append,
        monotonic=lambda: 0,
    )


def test_tiingo_spot_check_keeps_token_in_header_and_emits_ex_date_actions() -> None:
    transport = FakeTransport([_response([_row()])])
    result = _client(transport).fetch(
        symbol="AAPL",
        start=date(2024, 1, 4),
        end=date(2024, 1, 4),
    )
    actions = result.action_observations(security_id="apple-common")

    assert result.rows[0].close == Decimal("51.0")
    assert result.rows[0].adjusted_close == Decimal("25.5")
    assert result.rows[0].adjusted_volume == Decimal("2000")
    assert [action.action_type for action in actions] == ["split", "cash_dividend"]
    assert {action.effective_session for action in actions} == {date(2024, 1, 4)}
    assert actions[0].ratio_or_cash == Decimal("2.0")
    assert actions[1].ratio_or_cash == Decimal("1.0")
    url, headers, _ = transport.calls[0]
    assert "private-token" not in url
    assert headers["Authorization"] == "Token private-token"
    assert "startDate=2024-01-04" in url and "endDate=2024-01-04" in url


def test_tiingo_ex_date_comes_from_row_not_record_date_formula() -> None:
    transport = FakeTransport([_response([_row(day="2024-01-05T00:00:00Z", split=1)])])
    result = _client(transport).fetch(
        symbol="AAPL",
        start=date(2024, 1, 5),
        end=date(2024, 1, 5),
    )

    action = result.action_observations(security_id="apple-common")[0]
    assert action.action_type == "cash_dividend"
    assert action.effective_session == date(2024, 1, 5)


def test_tiingo_unknown_or_missing_row_field_fails_closed() -> None:
    missing = _row()
    missing.pop("splitFactor")
    transport = FakeTransport([_response([missing])])

    with pytest.raises(PricePayloadError, match="schema drifted"):
        _client(transport).fetch(
            symbol="AAPL",
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
        )


def test_tiingo_non_standard_json_number_fails_closed() -> None:
    transport = FakeTransport(
        [TiingoHttpResponse(200, {}, b'[{"date":"2024-01-04T00:00:00Z","open":NaN}]')]
    )

    with pytest.raises(PricePayloadError, match="non-standard constant"):
        _client(transport).fetch(
            symbol="AAPL",
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
        )


def test_tiingo_fractional_adjusted_volume_is_preserved_exactly() -> None:
    row = _row()
    row["adjVolume"] = 666.666
    transport = FakeTransport([_response([row])])

    result = _client(transport).fetch(
        symbol="AAPL",
        start=date(2024, 1, 4),
        end=date(2024, 1, 4),
    )

    assert result.rows[0].adjusted_volume == Decimal("666.666")


def test_tiingo_empty_spot_check_is_not_reported_as_success() -> None:
    transport = FakeTransport([_response([])])

    with pytest.raises(PricePayloadError, match="no EOD rows"):
        _client(transport).fetch(
            symbol="AAPL",
            start=date(2024, 1, 4),
            end=date(2024, 1, 4),
        )


def test_tiingo_retries_transient_status_without_leaking_credentials() -> None:
    sleeps: list[float] = []
    transport = FakeTransport([_response({}, status=429), _response([_row()])])
    result = _client(transport, sleeps=sleeps).fetch(
        symbol="AAPL",
        start=date(2024, 1, 4),
        end=date(2024, 1, 4),
    )

    assert len(result.rows) == 1
    assert len(transport.calls) == 2
    assert any(delay >= 1 for delay in sleeps)
    assert all("private-token" not in call[0] for call in transport.calls)
