"""Point-in-time Alpha Vantage listing snapshots with immutable local storage."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.config import AppConfig

ALPHA_LISTING_BASE_URL: Final = "https://www.alphavantage.co/query"
ALPHA_LISTING_PROVIDER: Final = "alpha_vantage"
ALPHA_LISTING_VERSION: Final = "usinv-alpha-listing-v1"
ALPHA_LISTING_HEADER: Final = (
    "symbol",
    "name",
    "exchange",
    "assetType",
    "ipoDate",
    "delistingDate",
    "status",
)
ALPHA_FREE_REQUESTS_PER_DAY: Final = 25
RETRIABLE_STATUS_CODES: Final = frozenset({429, 500, 502, 503, 504})
ListingState = Literal["active", "delisted"]


class ListingDataError(RuntimeError):
    """Base error for listing-source, parsing and storage failures."""


class ListingConfigurationError(ListingDataError):
    """Raised before networking when a listing request is unsafe."""


class ListingPayloadError(ListingDataError):
    """Raised when Alpha Vantage does not satisfy the registered CSV contract."""


class ListingStoreError(ListingDataError):
    """Raised when immutable listing evidence is incomplete or corrupted."""


@dataclass(frozen=True, slots=True)
class AlphaListingQuery:
    as_of: date
    state: ListingState

    def __post_init__(self) -> None:
        if self.as_of <= date(2010, 1, 1):
            raise ListingConfigurationError(
                "Alpha Vantage historical listing dates must be after 2010-01-01"
            )
        if self.state not in {"active", "delisted"}:
            raise ListingConfigurationError("listing state must be active or delisted")


@dataclass(frozen=True, slots=True)
class AlphaListingHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class AlphaListingTransport(Protocol):
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlphaListingHttpResponse:
        """Perform one HTTP GET without provider policy decisions."""


class UrllibAlphaListingTransport:
    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> AlphaListingHttpResponse:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return AlphaListingHttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    response.read(),
                )
        except HTTPError as exc:
            return AlphaListingHttpResponse(
                exc.code,
                dict(exc.headers.items()) if exc.headers is not None else {},
                exc.read(),
            )


@dataclass(frozen=True, slots=True)
class AlphaListingPage:
    query: AlphaListingQuery
    url: str
    retrieved_at: datetime
    content_sha256: str
    body: bytes

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ListingPayloadError("listing retrieval time must be timezone-aware")
        if "apikey=" in self.url.casefold() and "apikey=REDACTED" not in self.url:
            raise ListingPayloadError("listing evidence URL contains an unredacted credential")
        if hashlib.sha256(self.body).hexdigest() != self.content_sha256:
            raise ListingPayloadError("listing source hash does not match its body")


@dataclass(frozen=True, slots=True)
class AlphaListingRow:
    symbol: str
    name: str
    exchange: str
    asset_type: str
    ipo_date: date | None
    delisting_date: date | None
    status: Literal["Active", "Delisted"]
    state: ListingState
    row_number: int
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.exchange or not self.asset_type:
            raise ListingPayloadError("listing symbol, exchange and asset type cannot be empty")
        if self.symbol != self.symbol.strip().upper():
            raise ListingPayloadError("listing symbol is not normalized")
        if len(self.symbol) > 32 or any(character in self.symbol for character in "\r\n\0"):
            raise ListingPayloadError("listing symbol is unsafe")
        expected = "Active" if self.state == "active" else "Delisted"
        if self.status != expected:
            raise ListingPayloadError("listing row status does not match the requested state")
        if self.row_number < 2 or len(self.source_sha256) != 64:
            raise ListingPayloadError("listing row provenance is invalid")


@dataclass(frozen=True, slots=True)
class AlphaListingSnapshot:
    as_of: date
    pages: tuple[AlphaListingPage, ...]
    rows: tuple[AlphaListingRow, ...]
    version: str = ALPHA_LISTING_VERSION

    def __post_init__(self) -> None:
        if tuple(page.query.state for page in self.pages) != ("active", "delisted"):
            raise ListingPayloadError("listing snapshot requires active then delisted source pages")
        if any(page.query.as_of != self.as_of for page in self.pages):
            raise ListingPayloadError("listing page date does not match its snapshot")
        if not self.rows or {row.state for row in self.rows} != {"active", "delisted"}:
            raise ListingPayloadError(
                "listing snapshot requires non-empty active and delisted rows"
            )

    @property
    def snapshot_id(self) -> str:
        payload = {
            "version": self.version,
            "as_of": self.as_of.isoformat(),
            "pages": [
                {
                    "state": page.query.state,
                    "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
                    "sha256": page.content_sha256,
                }
                for page in self.pages
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _optional_date(value: str, *, field: str, row_number: int) -> date | None:
    normalized = value.strip()
    if normalized.casefold() in {"", "-", "n/a", "none", "null"}:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ListingPayloadError(f"listing {field} is invalid at CSV row {row_number}") from exc


def parse_alpha_listing_page(page: AlphaListingPage) -> tuple[AlphaListingRow, ...]:
    """Parse one exact CSV page; provider messages and schema drift fail closed."""
    try:
        text = page.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ListingPayloadError("listing response is not UTF-8 CSV") from exc
    reader = csv.DictReader(StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != ALPHA_LISTING_HEADER:
        raise ListingPayloadError("Alpha Vantage listing CSV header drifted")
    expected_status = "Active" if page.query.state == "active" else "Delisted"
    rows: list[AlphaListingRow] = []
    identities: set[tuple[object, ...]] = set()
    for row_number, raw in enumerate(reader, start=2):
        if None in raw or set(raw) != set(ALPHA_LISTING_HEADER):
            raise ListingPayloadError("Alpha Vantage listing CSV row width drifted")
        symbol = raw["symbol"].strip().upper()
        ipo_date = _optional_date(raw["ipoDate"], field="ipoDate", row_number=row_number)
        delisting_date = _optional_date(
            raw["delistingDate"], field="delistingDate", row_number=row_number
        )
        status = raw["status"].strip()
        if status != expected_status:
            raise ListingPayloadError("listing response contains an unexpected status")
        if ipo_date is not None and ipo_date > page.query.as_of:
            raise ListingPayloadError("listing response contains a post-cutoff IPO date")
        if delisting_date is not None and delisting_date > page.query.as_of:
            raise ListingPayloadError("listing response contains a post-cutoff delisting date")
        if page.query.state == "active" and delisting_date is not None:
            raise ListingPayloadError("active listing row unexpectedly has a delisting date")
        identity = (
            symbol,
            raw["exchange"].strip().upper(),
            raw["assetType"].strip(),
            ipo_date,
            delisting_date,
            status,
        )
        if identity in identities:
            raise ListingPayloadError("listing response contains an exact duplicate row")
        identities.add(identity)
        rows.append(
            AlphaListingRow(
                symbol=symbol,
                name=raw["name"].strip(),
                exchange=raw["exchange"].strip().upper(),
                asset_type=raw["assetType"].strip(),
                ipo_date=ipo_date,
                delisting_date=delisting_date,
                status=status,  # type: ignore[arg-type]
                state=page.query.state,
                row_number=row_number,
                source_sha256=page.content_sha256,
            )
        )
    if not rows:
        raise ListingPayloadError("Alpha Vantage listing CSV is empty")
    return tuple(rows)


class AlphaVantageListingClient:
    """Credential-safe, fail-closed client for two-state dated listing snapshots."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: AlphaListingTransport | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 30,
        max_attempts: int = 4,
        request_interval_seconds: float = 15,
        max_requests_per_day: int = ALPHA_FREE_REQUESTS_PER_DAY,
    ) -> None:
        self._api_key = api_key.strip()
        if not self._api_key or self._api_key.casefold() == "demo":
            raise ListingConfigurationError("a personal Alpha Vantage API key is required")
        if timeout_seconds <= 0 or max_attempts <= 0 or request_interval_seconds < 0:
            raise ListingConfigurationError("listing timeout, retries and pacing are invalid")
        if not 1 <= max_requests_per_day <= ALPHA_FREE_REQUESTS_PER_DAY:
            raise ListingConfigurationError("listing daily request cap must be between 1 and 25")
        self._transport = transport or UrllibAlphaListingTransport()
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._request_interval_seconds = request_interval_seconds
        self._max_requests_per_day = max_requests_per_day
        self._requests_by_utc_date: dict[date, int] = {}
        self._last_request_at: float | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: AppConfig, **kwargs: object) -> AlphaVantageListingClient:
        env_name = config.settings.credential_env.alpha_vantage_key
        return cls(os.environ.get(env_name, ""), **kwargs)

    def _pace_and_count(self) -> None:
        with self._lock:
            current = self._monotonic()
            if self._last_request_at is not None:
                delay = self._request_interval_seconds - (current - self._last_request_at)
                if delay > 0:
                    self._sleep(delay)
                    current = self._monotonic()
            utc_day = self._now().astimezone(UTC).date()
            used = self._requests_by_utc_date.get(utc_day, 0)
            if used >= self._max_requests_per_day:
                raise ListingConfigurationError("Alpha Vantage daily request budget is exhausted")
            self._requests_by_utc_date[utc_day] = used + 1
            self._last_request_at = current

    def _fetch_page(self, query: AlphaListingQuery) -> AlphaListingPage:
        public_params = {
            "function": "LISTING_STATUS",
            "date": query.as_of.isoformat(),
            "state": query.state,
        }
        request_url = (
            f"{ALPHA_LISTING_BASE_URL}?{urlencode({**public_params, 'apikey': self._api_key})}"
        )
        evidence_url = (
            f"{ALPHA_LISTING_BASE_URL}?{urlencode({**public_params, 'apikey': 'REDACTED'})}"
        )
        response: AlphaListingHttpResponse | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._pace_and_count()
            response = self._transport.get(
                request_url,
                {"User-Agent": "USInv/0.1 listing-ingest"},
                self._timeout_seconds,
            )
            if response.status == 200:
                break
            if response.status not in RETRIABLE_STATUS_CODES or attempt == self._max_attempts:
                raise ListingPayloadError(
                    f"Alpha Vantage listing request failed with HTTP {response.status}"
                )
            self._sleep(min(2 ** (attempt - 1), 8))
        assert response is not None
        page = AlphaListingPage(
            query=query,
            url=evidence_url,
            retrieved_at=self._now().astimezone(UTC),
            content_sha256=hashlib.sha256(response.body).hexdigest(),
            body=response.body,
        )
        parse_alpha_listing_page(page)
        return page

    def fetch_snapshot(self, *, as_of: date) -> AlphaListingSnapshot:
        pages = tuple(
            self._fetch_page(AlphaListingQuery(as_of, state)) for state in ("active", "delisted")
        )
        rows = tuple(row for page in pages for row in parse_alpha_listing_page(page))
        return AlphaListingSnapshot(as_of, pages, rows)


LISTING_ROWS_SCHEMA: Final = pa.schema(
    [
        ("symbol", pa.string()),
        ("name", pa.string()),
        ("exchange", pa.string()),
        ("asset_type", pa.string()),
        ("ipo_date", pa.date32()),
        ("delisting_date", pa.date32()),
        ("status", pa.string()),
        ("state", pa.string()),
        ("row_number", pa.int64()),
        ("source_sha256", pa.string()),
    ],
    metadata={b"usinv_table": b"alpha_vantage_listing_rows", b"schema_version": b"1"},
)


@dataclass(frozen=True, slots=True)
class AlphaListingSnapshotArtifact:
    snapshot_id: str
    output_dir: Path
    rows: int
    active_rows: int
    delisted_rows: int
    from_cache: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_listing_artifact(path: Path, snapshot: AlphaListingSnapshot) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ListingStoreError("listing snapshot manifest is unreadable") from exc
    if manifest.get("snapshot_id") != snapshot.snapshot_id:
        raise ListingStoreError("listing snapshot identity mismatch")
    pages = {page.query.state: page for page in snapshot.pages}
    for state, page in pages.items():
        raw_path = path / f"{state}.csv"
        if not raw_path.is_file() or _sha256(raw_path) != page.content_sha256:
            raise ListingStoreError(f"listing raw artifact failed verification: {state}")
    parquet_path = path / "listing_rows.parquet"
    artifact = manifest.get("artifacts", {}).get("listing_rows", {})
    if (
        not parquet_path.is_file()
        or _sha256(parquet_path) != artifact.get("sha256")
        or not pq.ParquetFile(parquet_path).schema_arrow.equals(
            LISTING_ROWS_SCHEMA, check_metadata=True
        )
    ):
        raise ListingStoreError("listing normalized artifact failed verification")


def materialize_alpha_listing_snapshot(
    snapshot: AlphaListingSnapshot,
    output_root: str | Path,
) -> AlphaListingSnapshotArtifact:
    """Store exact CSV pages and normalized rows under an immutable snapshot ID."""
    root = Path(output_root) / "alpha-vantage" / "listing-status" / snapshot.as_of.isoformat()
    target = root / snapshot.snapshot_id
    active_rows = sum(row.state == "active" for row in snapshot.rows)
    delisted_rows = len(snapshot.rows) - active_rows
    if target.exists():
        _verify_listing_artifact(target, snapshot)
        return AlphaListingSnapshotArtifact(
            snapshot.snapshot_id,
            target,
            len(snapshot.rows),
            active_rows,
            delisted_rows,
            True,
        )

    temporary = root / f".{snapshot.snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for page in snapshot.pages:
            (temporary / f"{page.query.state}.csv").write_bytes(page.body)
        table = pa.Table.from_pylist(
            [
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "exchange": row.exchange,
                    "asset_type": row.asset_type,
                    "ipo_date": row.ipo_date,
                    "delisting_date": row.delisting_date,
                    "status": row.status,
                    "state": row.state,
                    "row_number": row.row_number,
                    "source_sha256": row.source_sha256,
                }
                for row in snapshot.rows
            ],
            schema=LISTING_ROWS_SCHEMA,
        )
        parquet_path = temporary / "listing_rows.parquet"
        pq.write_table(table, parquet_path, compression="zstd")
        manifest = {
            "schema_version": 1,
            "version": snapshot.version,
            "provider": ALPHA_LISTING_PROVIDER,
            "snapshot_id": snapshot.snapshot_id,
            "as_of": snapshot.as_of.isoformat(),
            "raw_payload_policy": "private_local_artifact_only",
            "pages": [
                {
                    "state": page.query.state,
                    "url": page.url,
                    "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
                    "sha256": page.content_sha256,
                    "bytes": len(page.body),
                }
                for page in snapshot.pages
            ],
            "counts": {
                "rows": len(snapshot.rows),
                "active": active_rows,
                "delisted": delisted_rows,
            },
            "artifacts": {
                "listing_rows": {
                    "sha256": _sha256(parquet_path),
                    "rows": len(snapshot.rows),
                }
            },
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _verify_listing_artifact(target, snapshot)
    return AlphaListingSnapshotArtifact(
        snapshot.snapshot_id,
        target,
        len(snapshot.rows),
        active_rows,
        delisted_rows,
        False,
    )


def read_alpha_listing_snapshot(path: str | Path) -> AlphaListingSnapshot:
    """Open and verify a previously materialized private listing snapshot."""
    root = Path(path)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["schema_version"] != 1 or manifest["version"] != ALPHA_LISTING_VERSION:
            raise ValueError("schema/version")
        as_of = date.fromisoformat(manifest["as_of"])
        page_nodes = manifest["pages"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ListingStoreError("listing snapshot manifest is invalid") from exc
    if (
        not isinstance(page_nodes, list)
        or any(not isinstance(node, dict) for node in page_nodes)
        or [node.get("state") for node in page_nodes] != ["active", "delisted"]
    ):
        raise ListingStoreError("listing snapshot page manifest is invalid")
    pages: list[AlphaListingPage] = []
    try:
        for node in page_nodes:
            state = node["state"]
            body = (root / f"{state}.csv").read_bytes()
            pages.append(
                AlphaListingPage(
                    AlphaListingQuery(as_of, state),
                    node["url"],
                    datetime.fromisoformat(node["retrieved_at"]),
                    node["sha256"],
                    body,
                )
            )
    except (KeyError, OSError, TypeError, ValueError, ListingDataError) as exc:
        raise ListingStoreError("listing snapshot source pages are invalid") from exc
    rows = tuple(row for page in pages for row in parse_alpha_listing_page(page))
    snapshot = AlphaListingSnapshot(as_of, tuple(pages), rows)
    if manifest.get("snapshot_id") != snapshot.snapshot_id or root.name != snapshot.snapshot_id:
        raise ListingStoreError("listing snapshot identity does not match its directory")
    _verify_listing_artifact(root, snapshot)
    return snapshot
