from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from usinv.data.listings import (
    AlphaListingHttpResponse,
    AlphaListingPage,
    AlphaListingQuery,
    AlphaVantageListingClient,
    ListingConfigurationError,
    ListingPayloadError,
    ListingStoreError,
    materialize_alpha_listing_snapshot,
    parse_alpha_listing_page,
    read_alpha_listing_snapshot,
)

HEADER = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
ACTIVE = HEADER + "AAPL,Apple Inc.,NASDAQ,Stock,1980-12-12,,Active\n"
DELISTED = HEADER + "OLD,Old Corp,NYSE,Stock,2000-01-01,2020-01-02,Delisted\n"
NOW = datetime(2026, 7, 20, 10, tzinfo=UTC)


class FakeTransport:
    def __init__(self, responses: list[AlphaListingHttpResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlphaListingHttpResponse:
        assert headers == {"User-Agent": "USInv/0.1 listing-ingest"}
        assert timeout_seconds == 4
        self.urls.append(url)
        return self.responses.pop(0)


def _response(body: str, status: int = 200) -> AlphaListingHttpResponse:
    return AlphaListingHttpResponse(status, {"Content-Type": "text/csv"}, body.encode())


def _client(
    responses: list[AlphaListingHttpResponse],
    *,
    sleeps: list[float] | None = None,
    max_requests_per_day: int = 25,
    max_attempts: int = 4,
) -> tuple[AlphaVantageListingClient, FakeTransport]:
    transport = FakeTransport(responses)
    ticks = iter((0.0, 0.0, 15.0, 15.0, 30.0, 30.0))
    client = AlphaVantageListingClient(
        "private-key",
        transport=transport,
        now=lambda: NOW,
        sleep=(sleeps if sleeps is not None else []).append,
        monotonic=lambda: next(ticks),
        timeout_seconds=4,
        max_attempts=max_attempts,
        request_interval_seconds=15,
        max_requests_per_day=max_requests_per_day,
    )
    return client, transport


def test_listing_snapshot_is_dated_paced_and_never_exposes_credential() -> None:
    sleeps: list[float] = []
    client, transport = _client([_response(ACTIVE), _response(DELISTED)], sleeps=sleeps)

    snapshot = client.fetch_snapshot(as_of=date(2026, 7, 18))

    assert len(snapshot.rows) == 2
    assert tuple(page.query.state for page in snapshot.pages) == ("active", "delisted")
    assert all("apikey=REDACTED" in page.url for page in snapshot.pages)
    assert all("private-key" not in page.url for page in snapshot.pages)
    assert all("private-key" in url for url in transport.urls)
    assert sleeps == [15.0]
    assert snapshot.rows[0].ipo_date == date(1980, 12, 12)


def test_listing_schema_status_and_future_dates_fail_closed() -> None:
    query = AlphaListingQuery(date(2024, 1, 31), "active")

    def page(body: str) -> AlphaListingPage:
        payload = body.encode()
        import hashlib

        return AlphaListingPage(
            query,
            "https://www.alphavantage.co/query?apikey=REDACTED",
            NOW,
            hashlib.sha256(payload).hexdigest(),
            payload,
        )

    with pytest.raises(ListingPayloadError, match="header drifted"):
        parse_alpha_listing_page(page("symbol,name\nAAPL,Apple\n"))
    with pytest.raises(ListingPayloadError, match="unexpected status"):
        parse_alpha_listing_page(page(DELISTED))
    future = HEADER + "NEW,New Corp,NASDAQ,Stock,2025-01-01,,Active\n"
    with pytest.raises(ListingPayloadError, match="post-cutoff IPO"):
        parse_alpha_listing_page(page(future))

    blank_name = HEADER + "AAPL,,NASDAQ,Stock,1980-12-12,,Active\n"
    assert parse_alpha_listing_page(page(blank_name))[0].name == ""
    blank_exchange = HEADER + "AAPL,Apple Inc.,,Stock,1980-12-12,,Active\n"
    with pytest.raises(ListingPayloadError, match="symbol, exchange and asset type"):
        parse_alpha_listing_page(page(blank_exchange))


def test_listing_retry_is_bounded_and_error_does_not_leak_key() -> None:
    client, _transport = _client(
        [
            _response("rate limited", status=429),
            _response("still limited", status=429),
        ],
        max_requests_per_day=2,
        max_attempts=2,
    )

    with pytest.raises(ListingPayloadError, match="HTTP 429") as raised:
        client.fetch_snapshot(as_of=date(2026, 7, 18))

    assert "private-key" not in str(raised.value)


def test_listing_daily_budget_fails_before_an_untracked_request() -> None:
    client, transport = _client(
        [_response(ACTIVE), _response(DELISTED)],
        max_requests_per_day=1,
    )

    with pytest.raises(ListingConfigurationError, match="budget"):
        client.fetch_snapshot(as_of=date(2026, 7, 18))

    assert len(transport.urls) == 1


def test_listing_snapshot_is_content_addressed_and_corruption_is_detected(
    tmp_path: Path,
) -> None:
    client, _transport = _client([_response(ACTIVE), _response(DELISTED)])
    snapshot = client.fetch_snapshot(as_of=date(2026, 7, 18))

    created = materialize_alpha_listing_snapshot(snapshot, tmp_path)
    cached = materialize_alpha_listing_snapshot(snapshot, tmp_path)

    assert not created.from_cache and cached.from_cache
    assert created.snapshot_id == snapshot.snapshot_id
    assert created.active_rows == 1 and created.delisted_rows == 1
    reopened = read_alpha_listing_snapshot(created.output_dir)
    assert reopened.snapshot_id == snapshot.snapshot_id
    assert reopened.rows == snapshot.rows
    created.output_dir.joinpath("active.csv").write_bytes(b"tampered")
    with pytest.raises(ListingStoreError, match="raw artifact"):
        materialize_alpha_listing_snapshot(snapshot, tmp_path)
