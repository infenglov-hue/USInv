"""Versioned, checksummed archive for SEC Financial Statement Data Set ZIPs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from usinv import __version__
from usinv.config import AppConfig
from usinv.data.edgar.client import (
    RETRIABLE_STATUS_CODES,
    EdgarConfigurationError,
    EdgarError,
    EdgarHttpError,
    validate_sec_contact,
)

FSDS_BASE_URL: Final = "https://www.sec.gov/files/dera/data/financial-statement-data-sets"
FSDS_DATASET: Final = "sec_financial_statement_data_sets"
MANIFEST_SCHEMA_VERSION: Final = 1
REQUIRED_FSDS_MEMBERS: Final = frozenset({"sub.txt", "num.txt", "pre.txt", "tag.txt"})
_QUARTER_PATTERN: Final = re.compile(r"^(?P<year>\d{4})[qQ](?P<quarter>[1-4])$")
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_OUTCOMES: Final = frozenset({"initial", "unchanged", "reprocessed"})


class FsdsArchiveError(EdgarError):
    """Raised when FSDS archive state is incomplete, corrupt or contradictory."""


class FsdsPayloadError(FsdsArchiveError):
    """Raised when a downloaded payload is not a valid FSDS quarterly ZIP."""


@dataclass(frozen=True, order=True, slots=True)
class FsdsQuarter:
    """A validated SEC FSDS year/quarter identifier."""

    year: int
    quarter: int

    def __post_init__(self) -> None:
        if type(self.year) is not int or self.year < 2009:
            raise EdgarConfigurationError("FSDS year must be an integer >= 2009")
        if type(self.quarter) is not int or self.quarter not in {1, 2, 3, 4}:
            raise EdgarConfigurationError("FSDS quarter must be one of 1, 2, 3, 4")

    @classmethod
    def parse(cls, value: str) -> FsdsQuarter:
        match = _QUARTER_PATTERN.fullmatch(value.strip())
        if match is None:
            raise EdgarConfigurationError(
                f"invalid FSDS quarter {value!r}; expected YYYYqN (for example 2025q4)"
            )
        return cls(int(match.group("year")), int(match.group("quarter")))

    @property
    def label(self) -> str:
        return f"{self.year}q{self.quarter}"

    @property
    def url(self) -> str:
        return f"{FSDS_BASE_URL}/{self.label}.zip"

    def next(self) -> FsdsQuarter:
        if self.quarter == 4:
            return FsdsQuarter(self.year + 1, 1)
        return FsdsQuarter(self.year, self.quarter + 1)

    def __str__(self) -> str:
        return self.label


def fsds_quarter_range(start: FsdsQuarter | str, end: FsdsQuarter | str) -> tuple[FsdsQuarter, ...]:
    """Return an inclusive, chronological FSDS quarter range."""
    first = FsdsQuarter.parse(start) if isinstance(start, str) else start
    last = FsdsQuarter.parse(end) if isinstance(end, str) else end
    if first > last:
        raise EdgarConfigurationError(f"FSDS range starts after it ends: {first} > {last}")
    quarters: list[FsdsQuarter] = []
    current = first
    while current <= last:
        quarters.append(current)
        current = current.next()
    return tuple(quarters)


@dataclass(frozen=True, slots=True)
class BulkDownloadResponse:
    """Metadata for one streamed download attempt."""

    status: int
    headers: Mapping[str, str]
    byte_count: int = 0
    content_sha256: str | None = None


class BulkDownloadTransport(Protocol):
    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        destination: Path,
    ) -> BulkDownloadResponse:
        """Stream one HTTP response into ``destination`` without retrying."""


class UrllibBulkDownloadTransport:
    """Standard-library streaming transport that never buffers a full FSDS ZIP."""

    chunk_bytes: Final = 1024 * 1024

    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        destination: Path,
    ) -> BulkDownloadResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            response = urlopen(request, timeout=timeout_seconds)
        except HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers is not None else {}
            exc.close()
            return BulkDownloadResponse(status=exc.code, headers=response_headers)

        with response:
            response_headers = dict(response.headers.items())
            if response.status != 200:
                return BulkDownloadResponse(status=response.status, headers=response_headers)
            digest = hashlib.sha256()
            byte_count = 0
            with destination.open("xb") as output:
                while chunk := response.read(self.chunk_bytes):
                    output.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            return BulkDownloadResponse(
                status=response.status,
                headers=response_headers,
                byte_count=byte_count,
                content_sha256=digest.hexdigest(),
            )


FsdsOutcome = Literal["initial", "unchanged", "reprocessed"]


@dataclass(frozen=True, slots=True)
class FsdsArchiveRecord:
    """One append-only observation in the local FSDS immutability ledger."""

    quarter: FsdsQuarter
    source_url: str
    checked_at: datetime
    content_sha256: str
    byte_count: int
    object_path: str
    members: tuple[str, ...]
    outcome: FsdsOutcome
    previous_sha256: str | None
    http_status: int
    etag: str | None
    last_modified: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "quarter": self.quarter.label,
            "source_url": self.source_url,
            "checked_at": self.checked_at.astimezone(UTC).isoformat(),
            "content_sha256": self.content_sha256,
            "byte_count": self.byte_count,
            "object_path": self.object_path,
            "members": list(self.members),
            "outcome": self.outcome,
            "previous_sha256": self.previous_sha256,
            "http_status": self.http_status,
            "etag": self.etag,
            "last_modified": self.last_modified,
        }

    @classmethod
    def from_dict(cls, value: object) -> FsdsArchiveRecord:
        expected_keys = {
            "quarter",
            "source_url",
            "checked_at",
            "content_sha256",
            "byte_count",
            "object_path",
            "members",
            "outcome",
            "previous_sha256",
            "http_status",
            "etag",
            "last_modified",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise FsdsArchiveError("FSDS manifest record has an unexpected schema")
        try:
            quarter_raw = value["quarter"]
            source_url = value["source_url"]
            checked_raw = value["checked_at"]
            content_sha256 = value["content_sha256"]
            byte_count = value["byte_count"]
            object_path = value["object_path"]
            members_raw = value["members"]
            outcome = value["outcome"]
            previous_sha256 = value["previous_sha256"]
            http_status = value["http_status"]
            etag = value["etag"]
            last_modified = value["last_modified"]
            if not isinstance(quarter_raw, str) or not isinstance(checked_raw, str):
                raise TypeError
            if not all(isinstance(item, str) for item in (source_url, content_sha256, object_path)):
                raise TypeError
            if not isinstance(members_raw, list) or not all(
                isinstance(member, str) for member in members_raw
            ):
                raise TypeError
            if outcome not in _OUTCOMES:
                raise TypeError
            if previous_sha256 is not None and not isinstance(previous_sha256, str):
                raise TypeError
            if type(byte_count) is not int or type(http_status) is not int:
                raise TypeError
            if etag is not None and not isinstance(etag, str):
                raise TypeError
            if last_modified is not None and not isinstance(last_modified, str):
                raise TypeError
            checked_at = datetime.fromisoformat(checked_raw)
        except (TypeError, ValueError) as exc:
            raise FsdsArchiveError("FSDS manifest record contains invalid values") from exc
        if checked_at.tzinfo is None:
            raise FsdsArchiveError("FSDS manifest timestamp is timezone-naive")
        return cls(
            quarter=FsdsQuarter.parse(quarter_raw),
            source_url=source_url,
            checked_at=checked_at.astimezone(UTC),
            content_sha256=content_sha256,
            byte_count=byte_count,
            object_path=object_path,
            members=tuple(members_raw),
            outcome=outcome,
            previous_sha256=previous_sha256,
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
        )


@dataclass(frozen=True, slots=True)
class FsdsSyncResult:
    """Result of resolving one quarter from disk or SEC."""

    record: FsdsArchiveRecord
    network_accessed: bool
    new_version: bool

    @property
    def reprocessed(self) -> bool:
        return self.new_version and self.record.outcome == "reprocessed"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == expected), None)


class _RequestThrottle:
    def __init__(
        self,
        requests_per_second: float,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._interval = 1.0 / requests_per_second
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_allowed = 0.0

    def wait(self) -> None:
        now = self._monotonic()
        delay = self._next_allowed - now
        if delay > 0:
            self._sleep(delay)
            now = self._monotonic()
        self._next_allowed = max(now, self._next_allowed) + self._interval


class FsdsArchiveClient:
    """Download FSDS ZIPs without ever overwriting an observed raw version."""

    def __init__(
        self,
        *,
        contact_email: str,
        archive_dir: str | Path,
        max_requests_per_second: float = 2,
        timeout_seconds: float = 180,
        max_attempts: int = 4,
        backoff_base_seconds: float = 60,
        transport: BulkDownloadTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.contact_email = validate_sec_contact(contact_email)
        if not 0 < max_requests_per_second <= 8:
            raise EdgarConfigurationError("FSDS rate must be within (0, 8] requests/second")
        if timeout_seconds <= 0 or max_attempts < 1 or backoff_base_seconds < 0:
            raise EdgarConfigurationError("FSDS timeout, attempts or backoff is invalid")
        self.archive_dir = Path(archive_dir)
        self.user_agent = f"USInv/{__version__} {self.contact_email}"
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self._transport = transport or UrllibBulkDownloadTransport()
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._throttle = _RequestThrottle(max_requests_per_second, monotonic=monotonic, sleep=sleep)
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        archive_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> FsdsArchiveClient:
        env_name = config.settings.edgar.contact_env
        contact = os.environ.get(env_name)
        if contact is None or not contact.strip():
            raise EdgarConfigurationError(
                f"set {env_name} to a monitored contact email before using EDGAR"
            )
        resolved_archive = (
            Path(archive_dir)
            if archive_dir is not None
            else Path(config.settings.paths.data_dir) / "sec" / "fsds"
        )
        return cls(
            contact_email=contact,
            archive_dir=resolved_archive,
            max_requests_per_second=config.settings.edgar.max_requests_per_second,
            **kwargs,
        )

    @property
    def manifest_path(self) -> Path:
        return self.archive_dir / "manifest.json"

    @staticmethod
    def _relative_object_path(quarter: FsdsQuarter, content_sha256: str) -> str:
        return f"objects/{quarter.label}/{content_sha256}.zip"

    def _object_path(self, record: FsdsArchiveRecord) -> Path:
        relative = PurePosixPath(record.object_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise FsdsArchiveError("FSDS manifest contains an unsafe object path")
        path = self.archive_dir.joinpath(*relative.parts)
        try:
            path.resolve().relative_to(self.archive_dir.resolve())
        except ValueError as exc:
            raise FsdsArchiveError("FSDS object path escapes the archive") from exc
        return path

    def _load_manifest(self) -> tuple[FsdsArchiveRecord, ...]:
        if not self.manifest_path.exists():
            return ()
        if not self.manifest_path.is_file():
            raise FsdsArchiveError("FSDS manifest path is not a file")
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FsdsArchiveError("FSDS manifest is unreadable or invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "dataset", "events"}
            or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION
            or payload.get("dataset") != FSDS_DATASET
            or not isinstance(payload.get("events"), list)
        ):
            raise FsdsArchiveError("FSDS manifest header has an unexpected schema")
        records = tuple(FsdsArchiveRecord.from_dict(event) for event in payload["events"])
        self._validate_ledger(records)
        return records

    def _validate_ledger(self, records: Iterable[FsdsArchiveRecord]) -> None:
        previous_by_quarter: dict[FsdsQuarter, FsdsArchiveRecord] = {}
        for record in records:
            if not _SHA256_PATTERN.fullmatch(record.content_sha256):
                raise FsdsArchiveError("FSDS manifest contains an invalid SHA-256")
            if record.previous_sha256 is not None and not _SHA256_PATTERN.fullmatch(
                record.previous_sha256
            ):
                raise FsdsArchiveError("FSDS manifest contains an invalid previous SHA-256")
            if record.byte_count <= 0:
                raise FsdsArchiveError("FSDS manifest contains a non-positive byte count")
            if record.source_url != record.quarter.url:
                raise FsdsArchiveError("FSDS manifest source URL does not match its quarter")
            expected_path = self._relative_object_path(record.quarter, record.content_sha256)
            if record.object_path != expected_path:
                raise FsdsArchiveError("FSDS manifest object path does not match its hash")
            if not REQUIRED_FSDS_MEMBERS.issubset(record.members):
                raise FsdsArchiveError("FSDS manifest is missing required ZIP members")
            if tuple(sorted(set(record.members))) != record.members:
                raise FsdsArchiveError("FSDS manifest ZIP members are not unique and sorted")
            prior = previous_by_quarter.get(record.quarter)
            if prior is None:
                if record.outcome != "initial" or record.previous_sha256 is not None:
                    raise FsdsArchiveError("FSDS ledger must start each quarter with initial")
            else:
                if record.checked_at < prior.checked_at:
                    raise FsdsArchiveError("FSDS ledger timestamps run backwards")
                if record.previous_sha256 != prior.content_sha256:
                    raise FsdsArchiveError("FSDS ledger hash chain is broken")
                expected_outcome = (
                    "unchanged" if record.content_sha256 == prior.content_sha256 else "reprocessed"
                )
                if record.outcome != expected_outcome:
                    raise FsdsArchiveError("FSDS ledger outcome contradicts its hashes")
            if record.http_status not in {200, 304}:
                raise FsdsArchiveError("FSDS ledger contains an invalid HTTP status")
            if record.http_status == 304 and record.outcome != "unchanged":
                raise FsdsArchiveError("FSDS HTTP 304 can only record an unchanged package")
            previous_by_quarter[record.quarter] = record

    def _write_manifest(self, records: tuple[FsdsArchiveRecord, ...]) -> None:
        self._validate_ledger(records)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "dataset": FSDS_DATASET,
            "events": [record.to_dict() for record in records],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary = self.archive_dir / f".manifest.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _latest_record(
        records: Iterable[FsdsArchiveRecord], quarter: FsdsQuarter
    ) -> FsdsArchiveRecord | None:
        return next(
            (record for record in reversed(tuple(records)) if record.quarter == quarter), None
        )

    @staticmethod
    def _checked_now(now: Callable[[], datetime]) -> datetime:
        observed = now()
        if observed.tzinfo is None:
            raise FsdsArchiveError("FSDS archive clock must be timezone-aware")
        return observed.astimezone(UTC)

    @staticmethod
    def _inspect_zip(path: Path) -> tuple[str, int, tuple[str, ...]]:
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    byte_count += len(chunk)
            with zipfile.ZipFile(path) as archive:
                members: list[str] = []
                for info in archive.infolist():
                    member = PurePosixPath(info.filename)
                    if info.is_dir() or member.is_absolute() or len(member.parts) != 1:
                        raise FsdsPayloadError("FSDS ZIP contains an unsafe or nested member")
                    members.append(member.name.casefold())
                normalized_members = tuple(sorted(set(members)))
                if len(normalized_members) != len(members):
                    raise FsdsPayloadError("FSDS ZIP contains duplicate member names")
                missing = sorted(REQUIRED_FSDS_MEMBERS.difference(normalized_members))
                if missing:
                    raise FsdsPayloadError(f"FSDS ZIP is missing required members: {missing}")
                corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    raise FsdsPayloadError(f"FSDS ZIP failed CRC validation at {corrupt_member!r}")
        except (OSError, zipfile.BadZipFile) as exc:
            raise FsdsPayloadError("SEC returned an invalid FSDS ZIP") from exc
        if byte_count <= 0:
            raise FsdsPayloadError("SEC returned an empty FSDS ZIP")
        return digest.hexdigest(), byte_count, normalized_members

    def _verify_object(self, record: FsdsArchiveRecord) -> Path:
        path = self._object_path(record)
        if not path.is_file():
            raise FsdsArchiveError(f"FSDS archive object is missing for {record.quarter}")
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    byte_count += len(chunk)
        except OSError as exc:
            raise FsdsArchiveError(
                f"FSDS archive object is unreadable for {record.quarter}"
            ) from exc
        if byte_count != record.byte_count or digest.hexdigest() != record.content_sha256:
            raise FsdsArchiveError(f"FSDS archive object hash mismatch for {record.quarter}")
        return path

    def records(self) -> tuple[FsdsArchiveRecord, ...]:
        with self._lock:
            return self._load_manifest()

    def latest(self, quarter: FsdsQuarter | str) -> FsdsArchiveRecord | None:
        resolved = FsdsQuarter.parse(quarter) if isinstance(quarter, str) else quarter
        with self._lock:
            return self._latest_record(self._load_manifest(), resolved)

    def record_as_of(self, quarter: FsdsQuarter | str, as_of: datetime) -> FsdsArchiveRecord | None:
        """Select an archive version observed by ``as_of``, not fact availability."""
        if as_of.tzinfo is None:
            raise EdgarConfigurationError("FSDS as_of timestamp must be timezone-aware")
        resolved = FsdsQuarter.parse(quarter) if isinstance(quarter, str) else quarter
        cutoff = as_of.astimezone(UTC)
        with self._lock:
            eligible = tuple(
                record
                for record in self._load_manifest()
                if record.quarter == resolved and record.checked_at <= cutoff
            )
        return eligible[-1] if eligible else None

    def versions(self, quarter: FsdsQuarter | str) -> tuple[FsdsArchiveRecord, ...]:
        """Return the first observation of every distinct raw version."""
        resolved = FsdsQuarter.parse(quarter) if isinstance(quarter, str) else quarter
        seen: set[str] = set()
        distinct: list[FsdsArchiveRecord] = []
        with self._lock:
            records = self._load_manifest()
        for record in records:
            if record.quarter == resolved and record.content_sha256 not in seen:
                distinct.append(record)
                seen.add(record.content_sha256)
        return tuple(distinct)

    def audit(self) -> int:
        """Hash-check every distinct archived object and return the object count."""
        with self._lock:
            records = self._load_manifest()
            unique = {record.object_path: record for record in records}
            for record in unique.values():
                self._verify_object(record)
        return len(unique)

    def verify_record(self, record: FsdsArchiveRecord) -> Path:
        """Verify one manifest-backed object and return its safe local path."""
        with self._lock:
            records = self._load_manifest()
            if record not in records:
                raise FsdsArchiveError("FSDS record is not present in this archive manifest")
            return self._verify_object(record)

    def _headers(self, previous: FsdsArchiveRecord | None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/zip",
            "Accept-Encoding": "identity",
        }
        if previous is not None:
            if previous.etag:
                headers["If-None-Match"] = previous.etag
            if previous.last_modified:
                headers["If-Modified-Since"] = previous.last_modified
        return headers

    def _retry_delay(self, headers: Mapping[str, str], attempt: int) -> float:
        retry_after = (_header(headers, "Retry-After") or "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), 600.0)
        return min(self.backoff_base_seconds * (2**attempt), 600.0)

    def _download_path(self) -> Path:
        temporary_dir = self.archive_dir / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        return temporary_dir / f"{uuid.uuid4().hex}.part"

    def sync_quarter(self, quarter: FsdsQuarter | str, *, refresh: bool = False) -> FsdsSyncResult:
        """Resolve one quarter, optionally checking SEC for a replacement."""
        resolved = FsdsQuarter.parse(quarter) if isinstance(quarter, str) else quarter
        with self._lock:
            records = self._load_manifest()
            previous = self._latest_record(records, resolved)
            if previous is not None and not refresh:
                self._verify_object(previous)
                return FsdsSyncResult(previous, network_accessed=False, new_version=False)

            last_status: int | None = None
            for attempt in range(self.max_attempts):
                temporary = self._download_path()
                self._throttle.wait()
                try:
                    response = self._transport.download(
                        resolved.url,
                        self._headers(previous),
                        self.timeout_seconds,
                        temporary,
                    )
                except OSError as exc:
                    temporary.unlink(missing_ok=True)
                    if attempt + 1 == self.max_attempts:
                        raise EdgarHttpError(
                            None,
                            resolved.url,
                            f"FSDS network failure for {resolved.url}",
                        ) from exc
                    self._sleep(min(self.backoff_base_seconds * (2**attempt), 600.0))
                    continue

                last_status = response.status
                checked_at = self._checked_now(self._now)
                if previous is not None and checked_at < previous.checked_at:
                    temporary.unlink(missing_ok=True)
                    raise FsdsArchiveError("FSDS archive clock moved behind the prior observation")

                if response.status == 304:
                    temporary.unlink(missing_ok=True)
                    if previous is None:
                        raise FsdsArchiveError(
                            "SEC returned HTTP 304 without an archived FSDS object"
                        )
                    self._verify_object(previous)
                    record = FsdsArchiveRecord(
                        quarter=resolved,
                        source_url=resolved.url,
                        checked_at=checked_at,
                        content_sha256=previous.content_sha256,
                        byte_count=previous.byte_count,
                        object_path=previous.object_path,
                        members=previous.members,
                        outcome="unchanged",
                        previous_sha256=previous.content_sha256,
                        http_status=304,
                        etag=_header(response.headers, "ETag") or previous.etag,
                        last_modified=(
                            _header(response.headers, "Last-Modified") or previous.last_modified
                        ),
                    )
                    updated = (*records, record)
                    self._write_manifest(updated)
                    return FsdsSyncResult(record, network_accessed=True, new_version=False)

                if response.status == 200:
                    try:
                        content_sha256, byte_count, members = self._inspect_zip(temporary)
                        if (
                            response.content_sha256 is not None
                            and response.content_sha256 != content_sha256
                        ):
                            raise FsdsPayloadError("FSDS transport and archive SHA-256 disagree")
                        if response.byte_count not in {0, byte_count}:
                            raise FsdsPayloadError(
                                "FSDS transport and archive byte counts disagree"
                            )
                        previous_sha256 = previous.content_sha256 if previous is not None else None
                        if previous is None:
                            outcome: FsdsOutcome = "initial"
                        elif content_sha256 == previous.content_sha256:
                            outcome = "unchanged"
                        else:
                            outcome = "reprocessed"
                        relative_path = self._relative_object_path(resolved, content_sha256)
                        record = FsdsArchiveRecord(
                            quarter=resolved,
                            source_url=resolved.url,
                            checked_at=checked_at,
                            content_sha256=content_sha256,
                            byte_count=byte_count,
                            object_path=relative_path,
                            members=members,
                            outcome=outcome,
                            previous_sha256=previous_sha256,
                            http_status=200,
                            etag=_header(response.headers, "ETag"),
                            last_modified=_header(response.headers, "Last-Modified"),
                        )
                        object_path = self._object_path(record)
                        if object_path.exists():
                            self._verify_object(record)
                            temporary.unlink(missing_ok=True)
                        else:
                            object_path.parent.mkdir(parents=True, exist_ok=True)
                            temporary.replace(object_path)
                        updated = (*records, record)
                        self._write_manifest(updated)
                    except Exception:
                        temporary.unlink(missing_ok=True)
                        raise
                    return FsdsSyncResult(
                        record,
                        network_accessed=True,
                        new_version=outcome in {"initial", "reprocessed"},
                    )

                temporary.unlink(missing_ok=True)
                if (
                    response.status not in RETRIABLE_STATUS_CODES
                    or attempt + 1 == self.max_attempts
                ):
                    raise EdgarHttpError(
                        response.status,
                        resolved.url,
                        f"FSDS request failed with HTTP {response.status} for {resolved.url}",
                    )
                self._sleep(self._retry_delay(response.headers, attempt))

            raise EdgarHttpError(
                last_status,
                resolved.url,
                f"FSDS request exhausted retries for {resolved.url}",
            )

    def sync_range(
        self,
        start: FsdsQuarter | str,
        end: FsdsQuarter | str,
        *,
        refresh: bool = False,
    ) -> tuple[FsdsSyncResult, ...]:
        """Synchronize every quarter in an inclusive range, oldest first."""
        return tuple(
            self.sync_quarter(quarter, refresh=refresh)
            for quarter in fsds_quarter_range(start, end)
        )
