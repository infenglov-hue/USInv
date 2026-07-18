from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from usinv.data.edgar.bulk import (
    BulkDownloadResponse,
    FsdsArchiveClient,
    FsdsArchiveError,
    FsdsPayloadError,
    FsdsQuarter,
    fsds_quarter_range,
)
from usinv.data.edgar.client import EdgarConfigurationError


def _zip_bytes(marker: str = "v1", *, members: tuple[str, ...] | None = None) -> bytes:
    names = members or ("sub.txt", "num.txt", "pre.txt", "tag.txt")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, f"header\n{name}-{marker}\n")
    return output.getvalue()


class FakeBulkTransport:
    def __init__(
        self,
        *responses: tuple[int, Mapping[str, str], bytes | None] | OSError,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        destination: Path,
    ) -> BulkDownloadResponse:
        self.calls.append((url, dict(headers), timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, OSError):
            raise response
        status, response_headers, body = response
        if body is None:
            return BulkDownloadResponse(status=status, headers=response_headers)
        destination.write_bytes(body)
        return BulkDownloadResponse(
            status=status,
            headers=response_headers,
            byte_count=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
        )


def _client(
    tmp_path: Path,
    transport: FakeBulkTransport,
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] = lambda: 0.0,
) -> FsdsArchiveClient:
    kwargs: dict[str, object] = {}
    if now is not None:
        kwargs["now"] = now
    if sleep is not None:
        kwargs["sleep"] = sleep
    return FsdsArchiveClient(
        contact_email="ops@usinv.dev",
        archive_dir=tmp_path,
        transport=transport,
        max_requests_per_second=8,
        backoff_base_seconds=0,
        monotonic=monotonic,
        **kwargs,
    )


def test_quarter_parser_url_and_inclusive_range() -> None:
    quarter = FsdsQuarter.parse("2025Q4")
    assert quarter == FsdsQuarter(2025, 4)
    assert quarter.url.endswith("/2025q4.zip")
    assert fsds_quarter_range("2025q4", "2026q2") == (
        FsdsQuarter(2025, 4),
        FsdsQuarter(2026, 1),
        FsdsQuarter(2026, 2),
    )

    for invalid in ("2008q4", "2025q0", "2025q5", "2025-4"):
        with pytest.raises(EdgarConfigurationError):
            FsdsQuarter.parse(invalid)
    with pytest.raises(EdgarConfigurationError, match="starts after"):
        fsds_quarter_range("2026q2", "2026q1")


def test_initial_download_streams_to_hashed_object_and_writes_manifest(tmp_path: Path) -> None:
    body = _zip_bytes()
    transport = FakeBulkTransport(
        (200, {"ETag": '"v1"', "Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT"}, body)
    )
    client = _client(tmp_path, transport)

    result = client.sync_quarter("2026q1")

    assert result.network_accessed and result.new_version and not result.reprocessed
    assert result.record.outcome == "initial"
    assert result.record.content_sha256 == hashlib.sha256(body).hexdigest()
    assert (tmp_path / Path(result.record.object_path)).read_bytes() == body
    assert client.audit() == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["dataset"] == "sec_financial_statement_data_sets"
    assert len(manifest["events"]) == 1
    url, headers, timeout = transport.calls[0]
    assert url.endswith("/2026q1.zip") and timeout == 180
    assert headers["User-Agent"] == "USInv/0.1.0 ops@usinv.dev"
    assert headers["Accept-Encoding"] == "identity"


def test_archive_hit_avoids_network_and_corruption_fails_loudly(tmp_path: Path) -> None:
    transport = FakeBulkTransport((200, {}, _zip_bytes()))
    client = _client(tmp_path, transport)
    first = client.sync_quarter("2025q4")

    cached = client.sync_quarter("2025q4")
    assert not cached.network_accessed and not cached.new_version
    assert len(transport.calls) == 1

    (tmp_path / Path(first.record.object_path)).write_bytes(b"corrupt")
    with pytest.raises(FsdsArchiveError, match="hash mismatch"):
        client.sync_quarter("2025q4")


def test_reprocessing_preserves_old_object_and_as_of_blocks_future_version(
    tmp_path: Path,
) -> None:
    first_seen = datetime(2026, 1, 10, tzinfo=UTC)
    reprocessed_seen = datetime(2026, 7, 18, tzinfo=UTC)
    moments = iter((first_seen, reprocessed_seen))
    first_body = _zip_bytes("original")
    second_body = _zip_bytes("sec-reprocessed")
    transport = FakeBulkTransport((200, {}, first_body), (200, {}, second_body))
    client = _client(tmp_path, transport, now=lambda: next(moments))

    first = client.sync_quarter("2025q3")
    second = client.sync_quarter("2025q3", refresh=True)

    assert second.reprocessed and second.record.outcome == "reprocessed"
    assert second.record.previous_sha256 == first.record.content_sha256
    assert (tmp_path / Path(first.record.object_path)).read_bytes() == first_body
    assert (tmp_path / Path(second.record.object_path)).read_bytes() == second_body
    assert len(client.versions("2025q3")) == 2

    historical = client.record_as_of("2025q3", first_seen + timedelta(days=1))
    current = client.record_as_of("2025q3", reprocessed_seen)
    assert historical is not None and historical.content_sha256 == first.record.content_sha256
    assert current is not None and current.content_sha256 == second.record.content_sha256
    with pytest.raises(EdgarConfigurationError, match="timezone-aware"):
        client.record_as_of("2025q3", datetime(2026, 7, 18))


def test_unchanged_download_and_conditional_304_append_validation_events(tmp_path: Path) -> None:
    body = _zip_bytes()
    moments = iter(
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            datetime(2026, 7, 3, tzinfo=UTC),
        )
    )
    transport = FakeBulkTransport(
        (200, {"ETag": '"v1"'}, body),
        (200, {"ETag": '"v1"'}, body),
        (304, {"ETag": '"v1"'}, None),
    )
    client = _client(tmp_path, transport, now=lambda: next(moments))

    client.sync_quarter("2026q1")
    same_body = client.sync_quarter("2026q1", refresh=True)
    not_modified = client.sync_quarter("2026q1", refresh=True)

    assert same_body.record.outcome == "unchanged" and same_body.record.http_status == 200
    assert not_modified.record.outcome == "unchanged" and not_modified.record.http_status == 304
    assert not same_body.new_version and not not_modified.new_version
    assert len(client.records()) == 3
    assert len(client.versions("2026q1")) == 1
    assert transport.calls[1][1]["If-None-Match"] == '"v1"'
    assert transport.calls[2][1]["If-None-Match"] == '"v1"'


@pytest.mark.parametrize(
    "body",
    [
        b"not-a-zip",
        _zip_bytes(members=("sub.txt", "num.txt", "pre.txt")),
        _zip_bytes(members=("sub.txt", "num.txt", "pre.txt", "tag.txt", "../escape.txt")),
    ],
)
def test_invalid_or_unsafe_zip_is_rejected_without_ledger(tmp_path: Path, body: bytes) -> None:
    client = _client(tmp_path, FakeBulkTransport((200, {}, body)))

    with pytest.raises(FsdsPayloadError):
        client.sync_quarter("2026q1")

    assert not (tmp_path / "manifest.json").exists()
    assert not tuple(tmp_path.rglob("*.zip"))


def test_retriable_response_honors_retry_after(tmp_path: Path) -> None:
    sleeps: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    transport = FakeBulkTransport(
        (429, {"Retry-After": "2"}, None),
        (200, {}, _zip_bytes()),
    )
    client = _client(tmp_path, transport, sleep=sleep, monotonic=lambda: clock[0])

    assert client.sync_quarter("2026q1").record.outcome == "initial"
    assert sleeps == [2.0]
    assert len(transport.calls) == 2


def test_broken_manifest_hash_chain_is_rejected(tmp_path: Path) -> None:
    body = _zip_bytes()
    moments = iter((datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC)))
    client = _client(
        tmp_path,
        FakeBulkTransport((200, {}, body), (200, {}, body)),
        now=lambda: next(moments),
    )
    client.sync_quarter("2026q1")
    client.sync_quarter("2026q1", refresh=True)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["events"][1]["previous_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FsdsArchiveError, match="hash chain"):
        client.records()


def test_sync_range_downloads_every_quarter_in_order(tmp_path: Path) -> None:
    transport = FakeBulkTransport(
        (200, {}, _zip_bytes("q4")),
        (200, {}, _zip_bytes("q1")),
        (200, {}, _zip_bytes("q2")),
    )
    client = _client(tmp_path, transport)

    results = client.sync_range("2025q4", "2026q2")

    assert tuple(result.record.quarter.label for result in results) == (
        "2025q4",
        "2026q1",
        "2026q2",
    )
    assert all(result.new_version for result in results)
