from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.config import load_config
from usinv.data.edgar.client import (
    EdgarCacheError,
    EdgarClient,
    EdgarConfigurationError,
    EdgarHttpError,
    EdgarPayloadError,
    HttpResponse,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "edgar"


def _fixture(name: str) -> dict[str, object]:
    document = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return document["payload"]


SUBMISSIONS = _fixture("submissions_0000320193.json")
COMPANYFACTS = _fixture("companyfacts_0000320193.json")


def _response(
    payload: object,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers=headers or {},
        body=json.dumps(payload, separators=(",", ":")).encode(),
    )


class FakeTransport:
    def __init__(self, *responses: HttpResponse | OSError) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, Mapping[str, str], float]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        response = self.responses.popleft()
        if isinstance(response, OSError):
            raise response
        return response


def test_recorded_fixtures_have_live_provenance() -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        provenance = document["_fixture"]
        assert datetime.fromisoformat(provenance["retrieved_at"]).tzinfo is not None
        if "source_url" in provenance:
            assert provenance["source_url"].startswith("https://data.sec.gov/")
            assert len(provenance["raw_content_sha256"]) == 64
            assert "raw response omitted due size" in provenance["selection"]
        else:
            assert provenance["submissions_url"].startswith("https://data.sec.gov/")
            assert provenance["companyfacts_url"].startswith("https://data.sec.gov/")
            assert len(provenance["submissions_sha256"]) == 64
            assert len(provenance["companyfacts_sha256"]) == 64
            assert len(provenance["fsds_sha256"]) == 64


def _client(tmp_path: Path, transport: FakeTransport, **kwargs: object) -> EdgarClient:
    return EdgarClient(
        contact_email="ops@usinv.dev",
        cache_dir=tmp_path,
        transport=transport,
        backoff_base_seconds=0,
        **kwargs,
    )


def test_endpoints_declared_user_agent_and_fresh_cache(tmp_path: Path) -> None:
    transport = FakeTransport(_response(SUBMISSIONS), _response(COMPANYFACTS))
    client = _client(tmp_path, transport)

    first = client.submissions(320193)
    second = client.companyfacts("CIK0000320193")
    cached = client.submissions("0000320193")

    assert first.payload["name"] == "Apple Inc."
    assert second.payload["entityName"] == "Apple Inc."
    assert not first.from_cache
    assert cached.from_cache
    assert cached.content_sha256 == first.content_sha256
    assert len(transport.calls) == 2
    assert transport.calls[0][0].endswith("/submissions/CIK0000320193.json")
    assert transport.calls[1][0].endswith("/api/xbrl/companyfacts/CIK0000320193.json")
    assert transport.calls[0][1]["User-Agent"] == "USInv/0.1.0 ops@usinv.dev"
    assert transport.calls[0][1]["Accept-Encoding"] == "gzip, deflate"


def test_current_ticker_association_endpoint_is_explicitly_supported(tmp_path: Path) -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"]],
    }
    transport = FakeTransport(_response(payload))
    client = _client(tmp_path, transport)

    document = client.company_tickers_exchange()

    assert document.payload == payload
    assert transport.calls[0][0] == "https://www.sec.gov/files/company_tickers_exchange.json"
    assert transport.calls[0][1]["User-Agent"] == "USInv/0.1.0 ops@usinv.dev"


@pytest.mark.parametrize(
    ("value", "normalized"),
    [(320193, "0000320193"), ("320193", "0000320193"), ("CIK0000320193", "0000320193")],
)
def test_cik_normalization(value: int | str, normalized: str) -> None:
    assert EdgarClient.normalize_cik(value) == normalized


@pytest.mark.parametrize("value", [0, -1, "", "CIK", "12A", "12345678901"])
def test_invalid_cik_is_rejected_before_network(value: int | str) -> None:
    with pytest.raises(EdgarConfigurationError, match="invalid CIK"):
        EdgarClient.normalize_cik(value)


def test_config_requires_explicit_monitored_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config()
    monkeypatch.delenv(config.settings.edgar.contact_env, raising=False)

    with pytest.raises(EdgarConfigurationError, match=config.settings.edgar.contact_env):
        EdgarClient.from_config(config)

    for contact in ("not-an-email", "bot@example.com", "user@noreply.github.com"):
        with pytest.raises(EdgarConfigurationError, match="contact"):
            EdgarClient(contact_email=contact, cache_dir="unused")


def test_shared_spacing_enforces_configured_rate(tmp_path: Path) -> None:
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    transport = FakeTransport(_response(SUBMISSIONS), _response(COMPANYFACTS))
    client = _client(
        tmp_path,
        transport,
        max_requests_per_second=2,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    client.submissions(320193)
    client.companyfacts(320193)

    assert sleeps == [0.5]


@pytest.mark.parametrize(
    ("status", "headers", "expected_delay"),
    [(403, {}, 3.0), (429, {"retry-after": "2"}, 2.0)],
)
def test_403_and_429_back_off_then_retry(
    tmp_path: Path,
    status: int,
    headers: Mapping[str, str],
    expected_delay: float,
) -> None:
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    transport = FakeTransport(
        HttpResponse(status=status, headers=headers, body=b"busy"),
        _response(SUBMISSIONS),
    )
    client = EdgarClient(
        contact_email="ops@usinv.dev",
        cache_dir=tmp_path,
        transport=transport,
        max_attempts=2,
        backoff_base_seconds=3,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    assert client.submissions(320193).payload["name"] == "Apple Inc."
    assert sleeps == [expected_delay]
    assert len(transport.calls) == 2


def test_permanent_error_is_not_retried(tmp_path: Path) -> None:
    transport = FakeTransport(HttpResponse(status=404, headers={}, body=b"missing"))
    client = _client(tmp_path, transport)

    with pytest.raises(EdgarHttpError, match="HTTP 404") as error:
        client.submissions(320193)
    assert error.value.status == 404
    assert len(transport.calls) == 1


def test_stale_cache_revalidates_with_etag(tmp_path: Path) -> None:
    now = [datetime(2026, 7, 18, tzinfo=UTC)]
    transport = FakeTransport(
        _response(SUBMISSIONS, headers={"etag": '"fixture-v1"'}),
        HttpResponse(status=304, headers={}, body=b""),
    )
    client = _client(
        tmp_path,
        transport,
        cache_ttl_seconds=0,
        now=lambda: now[0],
    )

    fresh = client.submissions(320193)
    now[0] += timedelta(seconds=1)
    revalidated = client.submissions(320193)

    assert not fresh.from_cache
    assert revalidated.from_cache and revalidated.revalidated
    assert revalidated.content_sha256 == fresh.content_sha256
    assert revalidated.retrieved_at == fresh.retrieved_at
    assert revalidated.validated_at > fresh.validated_at
    assert transport.calls[1][1]["If-None-Match"] == '"fixture-v1"'


def test_corrupt_cache_fails_loudly(tmp_path: Path) -> None:
    transport = FakeTransport(_response(SUBMISSIONS))
    client = _client(tmp_path, transport)
    client.submissions(320193)
    next(tmp_path.glob("*.body.json")).write_bytes(b"tampered")

    with pytest.raises(EdgarCacheError, match="hash mismatch"):
        client.submissions(320193)


def test_invalid_json_is_not_cached(tmp_path: Path) -> None:
    transport = FakeTransport(HttpResponse(status=200, headers={}, body=b"not-json"))
    client = _client(tmp_path, transport)

    with pytest.raises(EdgarPayloadError, match="invalid JSON"):
        client.submissions(320193)
    assert not list(tmp_path.glob("*.body.json"))


def test_json_decimal_values_are_not_parsed_through_binary_float(tmp_path: Path) -> None:
    payload = {
        "cik": 1,
        "entityName": "Fixture",
        "facts": {"us-gaap": {"EarningsPerShareBasic": {"value": 0.1}}},
    }
    client = _client(tmp_path, FakeTransport(_response(payload)))

    document = client.companyfacts(1)

    assert document.payload["facts"]["us-gaap"]["EarningsPerShareBasic"]["value"] == Decimal("0.1")
