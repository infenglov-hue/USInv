"""Provider-neutral, security-keyed daily-price contracts and immutable storage."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.calendar import EXCHANGE_TIMEZONE, CalendarError, default_calendar
from usinv.data.edgar.securities import (
    SecurityMaster,
    SecurityMasterError,
    normalize_exchange,
    normalize_ticker,
)

Adjustment = Literal["raw", "all"]
PRICE_STORE_VERSION: Final = "usinv-prices-v1"


class PriceDataError(RuntimeError):
    """Base error for price-provider, mapping and storage contract violations."""


class PriceConfigurationError(PriceDataError):
    """Raised before networking when credentials or request bounds are unsafe."""


class PricePayloadError(PriceDataError):
    """Raised when a provider response cannot satisfy the declared schema."""


class PriceMappingError(PriceDataError):
    """Raised when mapped bars conflict or a mapping contract is malformed."""


class PriceStoreError(PriceDataError):
    """Raised when an immutable price snapshot is incomplete or corrupted."""


@dataclass(frozen=True, slots=True)
class PriceQuery:
    """One provider request whose bounds are exact timezone-aware instants."""

    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    adjustment: Adjustment
    feed: str = "sip"
    timeframe: str = "1Day"

    def __post_init__(self) -> None:
        try:
            normalized = tuple(sorted({normalize_ticker(symbol) for symbol in self.symbols}))
        except SecurityMasterError as exc:
            raise PriceConfigurationError("price request contains an invalid symbol") from exc
        if not normalized:
            raise PriceConfigurationError("at least one price symbol is required")
        if normalized != self.symbols:
            object.__setattr__(self, "symbols", normalized)
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise PriceConfigurationError("price request bounds must be timezone-aware")
        if self.start > self.end:
            raise PriceConfigurationError("price request start cannot follow end")
        if self.adjustment not in {"raw", "all"}:
            raise PriceConfigurationError("price adjustment must be raw or all")
        if self.feed != "sip":
            raise PriceConfigurationError("USInv daily price evidence must use consolidated SIP")
        if self.timeframe != "1Day":
            raise PriceConfigurationError("USInv canonical price bars must use timeframe=1Day")


@dataclass(frozen=True, slots=True)
class PriceSourcePage:
    """One exact raw provider page with retrieval provenance."""

    url: str
    retrieved_at: datetime
    content_sha256: str
    request_id: str | None
    body: bytes

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise PricePayloadError("source-page retrieval time must be timezone-aware")
        actual = hashlib.sha256(self.body).hexdigest()
        if actual != self.content_sha256:
            raise PricePayloadError("source-page content hash does not match its body")


@dataclass(frozen=True, slots=True)
class VendorDailyBar:
    """One exact provider daily bar before security-master resolution."""

    vendor_symbol: str
    timestamp: datetime
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int | None
    vwap: Decimal | None
    page_index: int

    def __post_init__(self) -> None:
        try:
            normalized_symbol = normalize_ticker(self.vendor_symbol)
        except SecurityMasterError as exc:
            raise PricePayloadError("vendor bar symbol is invalid") from exc
        if self.vendor_symbol != normalized_symbol:
            raise PricePayloadError("vendor bar symbol is not normalized")
        if self.timestamp.tzinfo is None:
            raise PricePayloadError("vendor bar timestamp must be timezone-aware")
        exchange_timestamp = self.timestamp.astimezone(EXCHANGE_TIMEZONE)
        if exchange_timestamp.date() != self.session:
            raise PricePayloadError("vendor bar timestamp does not match its XNYS session")
        values = (self.open, self.high, self.low, self.close)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            raise PricePayloadError("vendor OHLC values must be finite exact decimals")
        if self.vwap is not None and (
            not isinstance(self.vwap, Decimal) or not self.vwap.is_finite()
        ):
            raise PricePayloadError("vendor VWAP must be a finite exact decimal")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            raise PricePayloadError("vendor volume must be an integer")
        if self.trade_count is not None and (
            isinstance(self.trade_count, bool) or not isinstance(self.trade_count, int)
        ):
            raise PricePayloadError("vendor trade count must be an integer")
        if not isinstance(self.page_index, int) or isinstance(self.page_index, bool):
            raise PricePayloadError("vendor page index must be an integer")
        if (
            min(values) <= 0
            or (self.vwap is not None and self.vwap <= 0)
            or self.volume < 0
            or (self.trade_count or 0) < 0
            or (self.volume == 0 and ((self.trade_count or 0) != 0 or self.vwap is not None))
        ):
            raise PricePayloadError("vendor bar contains invalid price, volume, or trade fields")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise PricePayloadError("vendor bar violates OHLC bounds")
        if not self.low <= self.close <= self.high:
            raise PricePayloadError("vendor bar violates OHLC bounds")


@dataclass(frozen=True, slots=True)
class VendorBarIssue:
    """One symbol-local provider row that is archived but forbidden from prices."""

    vendor_symbol: str
    session: date
    kind: str
    detail: str
    page_index: int

    def __post_init__(self) -> None:
        try:
            normalized = normalize_ticker(self.vendor_symbol)
        except SecurityMasterError as exc:
            raise PricePayloadError("vendor bar issue symbol is invalid") from exc
        if (
            normalized != self.vendor_symbol
            or not self.kind
            or not self.detail
            or isinstance(self.page_index, bool)
            or not isinstance(self.page_index, int)
        ):
            raise PricePayloadError("vendor bar issue provenance is invalid")


@dataclass(frozen=True, slots=True)
class PriceFetchResult:
    """All pages and parsed bars returned for one provider query."""

    provider: str
    bar_definition: str
    query: PriceQuery
    pages: tuple[PriceSourcePage, ...]
    bars: tuple[VendorDailyBar, ...]
    provider_issues: tuple[VendorBarIssue, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider or not self.bar_definition or not self.pages:
            raise PricePayloadError("price result provenance is incomplete")
        if any(bar.page_index < 0 or bar.page_index >= len(self.pages) for bar in self.bars):
            raise PricePayloadError("price bar references an unknown source page")
        if any(bar.vendor_symbol not in self.query.symbols for bar in self.bars):
            raise PricePayloadError("price result contains an unrequested symbol")
        if any(
            issue.page_index < 0
            or issue.page_index >= len(self.pages)
            or issue.vendor_symbol not in self.query.symbols
            or issue.session < self.query.start.astimezone(EXCHANGE_TIMEZONE).date()
            or issue.session > self.query.end.astimezone(EXCHANGE_TIMEZONE).date()
            for issue in self.provider_issues
        ):
            raise PricePayloadError("price provider issue provenance is invalid")
        quarantined_symbols = {issue.vendor_symbol for issue in self.provider_issues}
        if any(bar.vendor_symbol in quarantined_symbols for bar in self.bars):
            raise PricePayloadError("quarantined provider symbol leaked into parsed bars")
        if any(
            bar.timestamp.astimezone(UTC) < self.query.start.astimezone(UTC)
            or bar.timestamp.astimezone(UTC) > self.query.end.astimezone(UTC)
            for bar in self.bars
        ):
            raise PricePayloadError("price result contains a bar outside its request bounds")
        try:
            for session in {
                *(bar.session for bar in self.bars),
                *(issue.session for issue in self.provider_issues),
            }:
                default_calendar().session(session)
        except CalendarError as exc:
            raise PricePayloadError("price result contains a non-XNYS session") from exc

    @property
    def source_sha256(self) -> str:
        digest = hashlib.sha256()
        for page in self.pages:
            digest.update(bytes.fromhex(page.content_sha256))
        return digest.hexdigest()

    @property
    def batch_id(self) -> str:
        payload = {
            "provider": self.provider,
            "bar_definition": self.bar_definition,
            "symbols": self.query.symbols,
            "start": self.query.start.astimezone(UTC).isoformat(),
            "end": self.query.end.astimezone(UTC).isoformat(),
            "adjustment": self.query.adjustment,
            "feed": self.query.feed,
            "timeframe": self.query.timeframe,
            "pages": [
                {
                    "url": page.url,
                    "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
                    "sha256": page.content_sha256,
                    "request_id": page.request_id,
                }
                for page in self.pages
            ],
            "provider_issues": [
                {
                    "vendor_symbol": issue.vendor_symbol,
                    "session": issue.session.isoformat(),
                    "kind": issue.kind,
                    "detail": issue.detail,
                    "page_index": issue.page_index,
                }
                for issue in self.provider_issues
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


class PriceProvider(ABC):
    """Provider interface; adapters return evidence, never security guesses."""

    @abstractmethod
    def fetch_daily_bars(self, query: PriceQuery) -> PriceFetchResult:
        """Fetch every page required by *query*."""


@dataclass(frozen=True, slots=True)
class PriceSecurityBinding:
    """A security-master ticker interval eligible to resolve provider bars."""

    security_id: str
    ticker: str
    exchange: str
    valid_from: date
    valid_to: date | None
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.security_id or not self.evidence_pointer:
            raise PriceMappingError("price binding identity evidence is incomplete")
        try:
            normalized_ticker = normalize_ticker(self.ticker)
            normalized_exchange = normalize_exchange(self.exchange)
        except SecurityMasterError as exc:
            raise PriceMappingError("price binding symbol or exchange is invalid") from exc
        if self.ticker != normalized_ticker:
            raise PriceMappingError("price binding ticker is not normalized")
        if self.exchange != normalized_exchange:
            raise PriceMappingError("price binding exchange is not normalized")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise PriceMappingError("price binding must be a non-empty half-open interval")

    def contains(self, session: date) -> bool:
        return self.valid_from <= session and (self.valid_to is None or session < self.valid_to)


@dataclass(frozen=True, slots=True)
class PriceMappingIssue:
    """A provider row deliberately kept out of canonical prices."""

    batch_id: str
    adjustment: Adjustment
    vendor_symbol: str
    session: date
    kind: str
    candidate_security_ids: tuple[str, ...]
    candidate_exchanges: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    snapshot_id: str
    output_dir: Path
    raw_rows: int
    adjusted_rows: int
    issues: tuple[PriceMappingIssue, ...]
    from_cache: bool


def price_bindings_from_security_master(
    master: SecurityMaster,
    *,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    minimum_confidence: Literal["low", "medium", "high"] = "high",
) -> tuple[PriceSecurityBinding, ...]:
    """Select dated, scoring-eligible security-master intervals for a price window."""
    if start > end:
        raise PriceMappingError("price binding window start cannot follow end")
    try:
        normalized = {normalize_ticker(symbol) for symbol in symbols}
    except SecurityMasterError as exc:
        raise PriceMappingError("price binding request contains an invalid symbol") from exc
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    threshold = confidence_order[minimum_confidence]
    bindings = {
        (
            interval.security_id,
            interval.ticker,
            interval.exchange,
            interval.valid_from,
            interval.valid_to,
            interval.evidence_pointer,
        ): PriceSecurityBinding(
            interval.security_id,
            interval.ticker,
            interval.exchange,
            interval.valid_from,
            interval.valid_to,
            interval.evidence_pointer,
        )
        for interval in master.symbols
        if interval.ticker in normalized
        and confidence_order[interval.confidence] >= threshold
        and interval.scope != "recovery_only"
        and interval.valid_from <= end
        and (interval.valid_to is None or interval.valid_to > start)
    }
    return tuple(
        sorted(
            bindings.values(),
            key=lambda item: (
                item.ticker,
                item.exchange,
                item.valid_from,
                item.valid_to or date.max,
                item.security_id,
                item.evidence_pointer,
            ),
        )
    )


_DECIMAL = pa.decimal128(28, 8)
_PRICE_FIELDS: Final = [
    ("batch_id", pa.string()),
    ("source_sha256", pa.string()),
    ("security_id", pa.string()),
    ("session", pa.date32()),
    ("provider_timestamp", pa.timestamp("us", tz="UTC")),
    ("open", _DECIMAL),
    ("high", _DECIMAL),
    ("low", _DECIMAL),
    ("close", _DECIMAL),
    ("volume", pa.int64()),
    ("trade_count", pa.int64()),
    ("vwap", _DECIMAL),
    ("provider", pa.string()),
    ("vendor_symbol", pa.string()),
    ("exchange", pa.string()),
    ("mapping_evidence", pa.string()),
    ("bar_definition", pa.string()),
    ("feed", pa.string()),
    ("observed_at", pa.timestamp("us", tz="UTC")),
    ("source_url", pa.string()),
    ("source_page_sha256", pa.string()),
    ("request_id", pa.string()),
]
PRICES_RAW_SCHEMA: Final = pa.schema(
    _PRICE_FIELDS,
    metadata={b"usinv_table": b"prices_raw", b"schema_version": b"1"},
)
PRICES_ADJUSTED_SCHEMA: Final = pa.schema(
    [*_PRICE_FIELDS, ("adjustment", pa.string())],
    metadata={b"usinv_table": b"prices_vendor_adjusted", b"schema_version": b"1"},
)
PRICE_ISSUES_SCHEMA: Final = pa.schema(
    [
        ("batch_id", pa.string()),
        ("adjustment", pa.string()),
        ("vendor_symbol", pa.string()),
        ("session", pa.date32()),
        ("kind", pa.string()),
        ("candidate_security_ids", pa.list_(pa.field("element", pa.string()))),
        ("candidate_exchanges", pa.list_(pa.field("element", pa.string()))),
        ("detail", pa.string()),
    ],
    metadata={b"usinv_table": b"price_mapping_issues", b"schema_version": b"1"},
)


def _query_contract(result: PriceFetchResult) -> tuple[object, ...]:
    query = result.query
    return (
        result.provider,
        result.bar_definition,
        query.symbols,
        query.start.astimezone(UTC),
        query.end.astimezone(UTC),
        query.feed,
        query.timeframe,
    )


def _resolve_binding(
    bar: VendorDailyBar,
    bindings: tuple[PriceSecurityBinding, ...],
) -> tuple[PriceSecurityBinding | None, tuple[str, ...], tuple[str, ...], str | None]:
    return _resolve_symbol_session(bar.vendor_symbol, bar.session, bindings)


def _resolve_symbol_session(
    vendor_symbol: str,
    session: date,
    bindings: tuple[PriceSecurityBinding, ...],
) -> tuple[PriceSecurityBinding | None, tuple[str, ...], tuple[str, ...], str | None]:
    matches = {
        (binding.security_id, binding.exchange, binding.evidence_pointer): binding
        for binding in bindings
        if binding.ticker == vendor_symbol and binding.contains(session)
    }
    security_ids = tuple(sorted({key[0] for key in matches}))
    exchanges = tuple(sorted({key[1] for key in matches}))
    if len(security_ids) == 1 and len(exchanges) == 1:
        candidates = sorted(
            (binding for binding in matches.values() if binding.security_id == security_ids[0]),
            key=lambda item: item.evidence_pointer,
        )
        evidence = "|".join(sorted({binding.evidence_pointer for binding in candidates}))
        return candidates[0], security_ids, exchanges, evidence
    return None, security_ids, exchanges, None


def _mapped_rows(
    result: PriceFetchResult,
    bindings: tuple[PriceSecurityBinding, ...],
) -> tuple[list[dict[str, object]], list[PriceMappingIssue]]:
    rows: dict[tuple[str, date, str, str], dict[str, object]] = {}
    issues: list[PriceMappingIssue] = []
    for issue in result.provider_issues:
        _, security_ids, exchanges, _ = _resolve_symbol_session(
            issue.vendor_symbol,
            issue.session,
            bindings,
        )
        issues.append(
            PriceMappingIssue(
                result.batch_id,
                result.query.adjustment,
                issue.vendor_symbol,
                issue.session,
                issue.kind,
                security_ids,
                exchanges,
                issue.detail,
            )
        )
    for bar in result.bars:
        binding, security_ids, exchanges, mapping_evidence = _resolve_binding(bar, bindings)
        if binding is None:
            kind = "unmapped" if not security_ids else "ambiguous_mapping"
            issues.append(
                PriceMappingIssue(
                    result.batch_id,
                    result.query.adjustment,
                    bar.vendor_symbol,
                    bar.session,
                    kind,
                    security_ids,
                    exchanges,
                    "bar requires exactly one security/exchange/date binding",
                )
            )
            continue
        page = result.pages[bar.page_index]
        row: dict[str, object] = {
            "batch_id": result.batch_id,
            "source_sha256": result.source_sha256,
            "security_id": binding.security_id,
            "session": bar.session,
            "provider_timestamp": bar.timestamp.astimezone(UTC),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "trade_count": bar.trade_count,
            "vwap": bar.vwap,
            "provider": result.provider,
            "vendor_symbol": bar.vendor_symbol,
            "exchange": binding.exchange,
            "mapping_evidence": mapping_evidence,
            "bar_definition": result.bar_definition,
            "feed": result.query.feed,
            "observed_at": page.retrieved_at.astimezone(UTC),
            "source_url": page.url,
            "source_page_sha256": page.content_sha256,
            "request_id": page.request_id,
        }
        if result.query.adjustment == "all":
            row["adjustment"] = "all"
        key = (binding.security_id, bar.session, result.provider, result.batch_id)
        previous = rows.get(key)
        if previous is not None and previous != row:
            raise PriceMappingError(
                "conflicting bars share a canonical security/session/provider/batch key"
            )
        rows[key] = row
    ordered = [rows[key] for key in sorted(rows)]
    return ordered, issues


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_snapshot(path: Path, snapshot_id: str) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PriceStoreError("price snapshot manifest is unreadable") from exc
    if manifest.get("snapshot_id") != snapshot_id:
        raise PriceStoreError("price snapshot identity mismatch")
    for name, schema in (
        ("prices_raw", PRICES_RAW_SCHEMA),
        ("prices_vendor_adjusted", PRICES_ADJUSTED_SCHEMA),
        ("price_mapping_issues", PRICE_ISSUES_SCHEMA),
    ):
        artifact = manifest.get("artifacts", {}).get(name, {})
        artifact_path = path / f"{name}.parquet"
        if (
            not artifact_path.is_file()
            or _sha256(artifact_path) != artifact.get("sha256")
            or not pq.ParquetFile(artifact_path).schema_arrow.equals(schema, check_metadata=True)
            or pq.ParquetFile(artifact_path).metadata.num_rows != artifact.get("rows")
        ):
            raise PriceStoreError(f"price snapshot artifact failed verification: {name}")
    for source in manifest.get("source_pages", []):
        source_path = path / source["path"]
        if not source_path.is_file() or _sha256(source_path) != source.get("sha256"):
            raise PriceStoreError("price source-page archive failed verification")


def materialize_price_snapshot(
    raw: PriceFetchResult,
    adjusted: PriceFetchResult,
    bindings: tuple[PriceSecurityBinding, ...],
    output_root: str | Path,
) -> PriceSnapshot:
    """Map raw/all bars and write one immutable, content-addressed local snapshot."""
    if raw.query.adjustment != "raw" or adjusted.query.adjustment != "all":
        raise PriceStoreError("materialization requires separate raw and all provider results")
    if _query_contract(raw) != _query_contract(adjusted):
        raise PriceStoreError("raw and adjusted fetches must have identical provider/query bounds")
    raw_rows, raw_issues = _mapped_rows(raw, bindings)
    adjusted_rows, adjusted_issues = _mapped_rows(adjusted, bindings)
    issues = tuple(
        sorted(
            [*raw_issues, *adjusted_issues],
            key=lambda item: (
                item.adjustment,
                item.vendor_symbol,
                item.session,
                item.kind,
            ),
        )
    )
    binding_payload = [
        {
            "security_id": item.security_id,
            "ticker": item.ticker,
            "exchange": item.exchange,
            "valid_from": item.valid_from.isoformat(),
            "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            "evidence_pointer": item.evidence_pointer,
        }
        for item in sorted(
            bindings,
            key=lambda item: (
                item.ticker,
                item.exchange,
                item.valid_from,
                item.security_id,
            ),
        )
    ]
    identity = {
        "store_version": PRICE_STORE_VERSION,
        "raw_batch_id": raw.batch_id,
        "adjusted_batch_id": adjusted.batch_id,
        "bindings": binding_payload,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    snapshot_id = hashlib.sha256(canonical).hexdigest()
    root = Path(output_root) / "snapshots"
    target = root / snapshot_id
    if target.exists():
        _verify_snapshot(target, snapshot_id)
        return PriceSnapshot(snapshot_id, target, len(raw_rows), len(adjusted_rows), issues, True)

    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        pq.write_table(
            pa.Table.from_pylist(raw_rows, schema=PRICES_RAW_SCHEMA),
            temporary / "prices_raw.parquet",
        )
        pq.write_table(
            pa.Table.from_pylist(adjusted_rows, schema=PRICES_ADJUSTED_SCHEMA),
            temporary / "prices_vendor_adjusted.parquet",
        )
        issue_rows = [
            {
                "batch_id": issue.batch_id,
                "adjustment": issue.adjustment,
                "vendor_symbol": issue.vendor_symbol,
                "session": issue.session,
                "kind": issue.kind,
                "candidate_security_ids": list(issue.candidate_security_ids),
                "candidate_exchanges": list(issue.candidate_exchanges),
                "detail": issue.detail,
            }
            for issue in issues
        ]
        pq.write_table(
            pa.Table.from_pylist(issue_rows, schema=PRICE_ISSUES_SCHEMA),
            temporary / "price_mapping_issues.parquet",
        )
        artifacts = {}
        for name in ("prices_raw", "prices_vendor_adjusted", "price_mapping_issues"):
            artifact_path = temporary / f"{name}.parquet"
            artifacts[name] = {
                "sha256": _sha256(artifact_path),
                "bytes": artifact_path.stat().st_size,
                "rows": pq.ParquetFile(artifact_path).metadata.num_rows,
            }
        source_dir = temporary / "source_pages"
        source_dir.mkdir()
        source_pages: list[dict[str, object]] = []
        for result in (raw, adjusted):
            for index, page in enumerate(result.pages):
                filename = f"{result.query.adjustment}-{index:04d}-{page.content_sha256}.json"
                source_path = source_dir / filename
                source_path.write_bytes(page.body)
                source_pages.append(
                    {
                        "adjustment": result.query.adjustment,
                        "index": index,
                        "path": f"source_pages/{filename}",
                        "url": page.url,
                        "retrieved_at": page.retrieved_at.astimezone(UTC).isoformat(),
                        "request_id": page.request_id,
                        "sha256": _sha256(source_path),
                        "bytes": source_path.stat().st_size,
                    }
                )
        manifest = {
            "schema_version": 1,
            "store_version": PRICE_STORE_VERSION,
            "snapshot_id": snapshot_id,
            **identity,
            "artifacts": artifacts,
            "source_pages": source_pages,
            "counts": {
                "raw_provider_rows": len(raw.bars),
                "raw_mapped_rows": len(raw_rows),
                "adjusted_provider_rows": len(adjusted.bars),
                "adjusted_mapped_rows": len(adjusted_rows),
                "mapping_issues": len(issues),
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
    _verify_snapshot(target, snapshot_id)
    return PriceSnapshot(snapshot_id, target, len(raw_rows), len(adjusted_rows), issues, False)


def read_price_snapshot(path: str | Path) -> PriceSnapshot:
    """Open a verified immutable price snapshot without refetching provider data."""
    root = Path(path)
    snapshot_id = root.name
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise PriceStoreError("price snapshot directory is not content-addressed")
    _verify_snapshot(root, snapshot_id)
    try:
        raw_rows = pq.ParquetFile(root / "prices_raw.parquet").metadata.num_rows
        adjusted_rows = pq.ParquetFile(root / "prices_vendor_adjusted.parquet").metadata.num_rows
        issue_rows = pq.read_table(root / "price_mapping_issues.parquet").to_pylist()
        issues = tuple(
            PriceMappingIssue(
                row["batch_id"],
                row["adjustment"],
                row["vendor_symbol"],
                row["session"],
                row["kind"],
                tuple(row["candidate_security_ids"]),
                tuple(row["candidate_exchanges"]),
                row["detail"],
            )
            for row in issue_rows
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PriceStoreError("price snapshot rows are invalid") from exc
    return PriceSnapshot(snapshot_id, root, raw_rows, adjusted_rows, issues, True)
