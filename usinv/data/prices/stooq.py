"""Stooq adjusted-only bulk archive importer and empirical basis gate."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from usinv.calendar import CalendarError, default_calendar
from usinv.data.edgar.securities import SecurityMasterError, normalize_ticker
from usinv.data.prices.base import PriceConfigurationError, PricePayloadError, PriceStoreError

STOOQ_BULK_URL: Final = "https://stooq.com/db/d/?b=d_us_txt"
STOOQ_PROVIDER: Final = "stooq"
STOOQ_BAR_DEFINITION: Final = "stooq-d-us-ascii-adjusted-undocumented-basis"
STOOQ_EXPECTED_HEADER: Final = (
    "<TICKER>",
    "<PER>",
    "<DATE>",
    "<TIME>",
    "<OPEN>",
    "<HIGH>",
    "<LOW>",
    "<CLOSE>",
    "<VOL>",
    "<OPENINT>",
)
MAX_ARCHIVE_ENTRIES: Final = 100_000
MAX_ENTRY_BYTES: Final = 256 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES: Final = 32 * 1024 * 1024 * 1024
DRIFT_TOLERANCE: Final = Decimal("0.01")

StooqAdjustmentBasis = Literal["splits_only", "splits_and_dividends", "unresolved"]


@dataclass(frozen=True, slots=True)
class StooqBulkArchive:
    source_path: Path
    source_url: str
    retrieved_at: datetime
    content_sha256: str
    bytes: int

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise PriceStoreError("Stooq retrieval time must be timezone-aware")
        if self.source_url != STOOQ_BULK_URL:
            raise PriceStoreError("Stooq bulk source URL does not match the registered contract")
        if not self.source_path.is_file() or self.bytes <= 0:
            raise PriceStoreError("Stooq bulk archive is missing or empty")


@dataclass(frozen=True, slots=True)
class StooqAdjustedBar:
    vendor_symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    archive_sha256: str
    archive_entry: str
    row_number: int

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= 0 for value in values):
            raise PricePayloadError("Stooq OHLC values must be finite and positive")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise PricePayloadError("Stooq OHLC bounds are invalid")
        if not self.low <= self.close <= self.high:
            raise PricePayloadError("Stooq OHLC bounds are invalid")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume <= 0:
            raise PricePayloadError("Stooq volume must be a positive integer")


@dataclass(frozen=True, slots=True)
class StooqBulkResult:
    archive: StooqBulkArchive
    symbols: tuple[str, ...]
    bars: tuple[StooqAdjustedBar, ...]
    adjustment_basis: StooqAdjustmentBasis = "unresolved"


@dataclass(frozen=True, slots=True)
class StooqBasisSample:
    security_id: str
    session: date
    event_type: Literal["split", "cash_dividend"]
    stooq_close: Decimal
    split_only_close: Decimal
    total_return_close: Decimal
    evidence_pointer: str

    def __post_init__(self) -> None:
        values = (self.stooq_close, self.split_only_close, self.total_return_close)
        if not self.security_id or not self.evidence_pointer:
            raise PricePayloadError("Stooq basis evidence identity is incomplete")
        if any(not value.is_finite() or value <= 0 for value in values):
            raise PricePayloadError("Stooq basis prices must be finite and positive")


@dataclass(frozen=True, slots=True)
class StooqBasisAssessment:
    basis: StooqAdjustmentBasis
    sample_count: int
    dividend_sample_count: int
    split_sample_count: int
    evidence_pointers: tuple[str, ...]
    reason: str

    @property
    def drift_check_enabled(self) -> bool:
        return self.basis != "unresolved"


@dataclass(frozen=True, slots=True)
class StooqDriftIssue:
    security_id: str
    session: date
    relative_drift: Decimal
    evidence_pointer: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
                raise PricePayloadError("Stooq archive entry count is unsafe")
            total = 0
            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                path = PurePosixPath(normalized_name)
                parts = path.parts
                if (
                    entry.flag_bits & 0x1
                    or not parts
                    or path.is_absolute()
                    or ":" in parts[0]
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise PricePayloadError("Stooq archive contains an unsafe entry")
                if entry.file_size > MAX_ENTRY_BYTES:
                    raise PricePayloadError("Stooq archive entry exceeds the safety limit")
                total += entry.file_size
                if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise PricePayloadError("Stooq archive exceeds the uncompressed safety limit")
            bad = archive.testzip()
            if bad is not None:
                raise PricePayloadError(f"Stooq archive CRC failed: {bad}")
    except zipfile.BadZipFile as exc:
        raise PricePayloadError(
            "Stooq response is not a ZIP archive (automation challenge or schema drift)"
        ) from exc


def archive_stooq_bulk(
    source_path: str | Path,
    output_root: str | Path,
    *,
    retrieved_at: datetime,
    source_url: str = STOOQ_BULK_URL,
) -> StooqBulkArchive:
    """Copy one full download into immutable content-addressed storage; never append."""
    source = Path(source_path)
    if retrieved_at.tzinfo is None:
        raise PriceConfigurationError("Stooq retrieved_at must be timezone-aware")
    if source_url != STOOQ_BULK_URL:
        raise PriceConfigurationError(
            "Stooq bulk source URL does not match the registered contract"
        )
    if not source.is_file():
        raise PriceStoreError("Stooq source archive does not exist")
    _validate_zip(source)
    digest = _sha256(source)
    root = Path(output_root) / "stooq" / digest
    target = root / "d_us_txt.zip"
    manifest_path = root / "manifest.json"
    if root.exists():
        if not target.is_file() or _sha256(target) != digest:
            raise PriceStoreError("cached Stooq archive failed hash verification")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_source_url = manifest["source_url"]
            manifest_retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
            manifest_sha256 = manifest["sha256"]
            manifest_bytes = manifest["bytes"]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PriceStoreError("cached Stooq manifest is invalid") from exc
        if (
            manifest_source_url != STOOQ_BULK_URL
            or manifest_sha256 != digest
            or manifest_bytes != target.stat().st_size
            or manifest_retrieved_at.tzinfo is None
        ):
            raise PriceStoreError("cached Stooq manifest does not match the immutable archive")
        return StooqBulkArchive(
            target,
            manifest_source_url,
            manifest_retrieved_at.astimezone(UTC),
            digest,
            target.stat().st_size,
        )

    temporary = root.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        temporary_target = temporary / "d_us_txt.zip"
        shutil.copyfile(source, temporary_target)
        if _sha256(temporary_target) != digest:
            raise PriceStoreError("Stooq archive changed while copying")
        manifest = {
            "schema_version": 1,
            "provider": STOOQ_PROVIDER,
            "bar_definition": STOOQ_BAR_DEFINITION,
            "source_url": source_url,
            "retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
            "sha256": digest,
            "bytes": temporary_target.stat().st_size,
            "replacement_semantics": "full_redownload_never_append",
            "adjustment_basis": "unresolved_until_empirical_dividend_test",
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    if not manifest_path.is_file():
        raise PriceStoreError("Stooq archive manifest was not materialized")
    return StooqBulkArchive(
        target,
        source_url,
        retrieved_at.astimezone(UTC),
        digest,
        target.stat().st_size,
    )


def _decimal(value: str, *, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise PricePayloadError(f"Stooq {field} is not an exact decimal") from exc
    if not parsed.is_finite():
        raise PricePayloadError(f"Stooq {field} must be finite")
    return parsed


def _normalize_stooq_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[:-3]
    try:
        return normalize_ticker(symbol)
    except SecurityMasterError as exc:
        raise PricePayloadError("Stooq row contains an invalid ticker") from exc


def read_stooq_bulk(
    archive: StooqBulkArchive,
    *,
    symbols: tuple[str, ...],
) -> StooqBulkResult:
    """Read only requested symbols from the full adjusted-only ASCII archive."""
    try:
        normalized = tuple(sorted({normalize_ticker(symbol) for symbol in symbols}))
    except SecurityMasterError as exc:
        raise PriceConfigurationError("Stooq request contains an invalid symbol") from exc
    if not normalized:
        raise PriceConfigurationError("Stooq request requires at least one symbol")
    requested = set(normalized)
    bars: list[StooqAdjustedBar] = []
    _validate_zip(archive.source_path)
    with zipfile.ZipFile(archive.source_path) as bundle:
        for entry in sorted(bundle.infolist(), key=lambda item: item.filename):
            if entry.is_dir() or not entry.filename.casefold().endswith((".txt", ".csv")):
                continue
            with bundle.open(entry) as raw_file:
                lines = (line.decode("ascii", errors="strict") for line in raw_file)
                reader = csv.reader(lines)
                try:
                    header = tuple(next(reader))
                except StopIteration:
                    continue
                if header != STOOQ_EXPECTED_HEADER:
                    raise PricePayloadError("Stooq ASCII header drifted")
                for row_number, row in enumerate(reader, start=2):
                    if len(row) != len(STOOQ_EXPECTED_HEADER):
                        raise PricePayloadError("Stooq ASCII row width drifted")
                    symbol = _normalize_stooq_symbol(row[0])
                    if symbol not in requested:
                        continue
                    if row[1] != "D" or row[3] not in {"", "000000"}:
                        raise PricePayloadError("Stooq row is not a canonical daily bar")
                    try:
                        session = datetime.strptime(row[2], "%Y%m%d").date()
                        default_calendar().session(session)
                        volume = int(row[8])
                    except (ValueError, CalendarError) as exc:
                        raise PricePayloadError("Stooq row date/volume is invalid") from exc
                    bars.append(
                        StooqAdjustedBar(
                            symbol,
                            session,
                            _decimal(row[4], field="open"),
                            _decimal(row[5], field="high"),
                            _decimal(row[6], field="low"),
                            _decimal(row[7], field="close"),
                            volume,
                            archive.content_sha256,
                            entry.filename,
                            row_number,
                        )
                    )
    bars.sort(key=lambda item: (item.vendor_symbol, item.session, item.archive_entry))
    if len({(bar.vendor_symbol, bar.session) for bar in bars}) != len(bars):
        raise PricePayloadError("Stooq archive has duplicate requested symbol/session rows")
    return StooqBulkResult(archive, normalized, tuple(bars))


def _relative_error(observed: Decimal, expected: Decimal) -> Decimal:
    return abs(observed - expected) / expected


def classify_stooq_adjustment_basis(
    samples: tuple[StooqBasisSample, ...],
) -> StooqBasisAssessment:
    """Enable drift checks only after split and dividend-payer evidence separates bases."""
    dividend_samples = tuple(sample for sample in samples if sample.event_type == "cash_dividend")
    split_samples = tuple(sample for sample in samples if sample.event_type == "split")
    pointers = tuple(sorted({sample.evidence_pointer for sample in samples}))
    if not dividend_samples or not split_samples:
        return StooqBasisAssessment(
            "unresolved",
            len(samples),
            len(dividend_samples),
            len(split_samples),
            pointers,
            "both a declared split and a dividend-payer sample are required",
        )
    split_valid = all(
        min(
            _relative_error(sample.stooq_close, sample.split_only_close),
            _relative_error(sample.stooq_close, sample.total_return_close),
        )
        <= DRIFT_TOLERANCE
        for sample in split_samples
    )
    if not split_valid:
        return StooqBasisAssessment(
            "unresolved",
            len(samples),
            len(dividend_samples),
            len(split_samples),
            pointers,
            "split sample does not match either reconstructed basis",
        )
    split_only_wins = all(
        _relative_error(sample.stooq_close, sample.split_only_close) <= DRIFT_TOLERANCE
        and _relative_error(sample.stooq_close, sample.split_only_close)
        < _relative_error(sample.stooq_close, sample.total_return_close)
        for sample in dividend_samples
    )
    total_return_wins = all(
        _relative_error(sample.stooq_close, sample.total_return_close) <= DRIFT_TOLERANCE
        and _relative_error(sample.stooq_close, sample.total_return_close)
        < _relative_error(sample.stooq_close, sample.split_only_close)
        for sample in dividend_samples
    )
    if split_only_wins == total_return_wins:
        basis: StooqAdjustmentBasis = "unresolved"
        reason = "dividend samples do not uniquely identify the Stooq adjustment basis"
    elif split_only_wins:
        basis = "splits_only"
        reason = "declared dividend samples match the independently split-only series"
    else:
        basis = "splits_and_dividends"
        reason = "declared dividend samples match the independently total-return series"
    return StooqBasisAssessment(
        basis,
        len(samples),
        len(dividend_samples),
        len(split_samples),
        pointers,
        reason,
    )


def check_stooq_drift(
    samples: tuple[StooqBasisSample, ...],
    assessment: StooqBasisAssessment,
) -> tuple[StooqDriftIssue, ...]:
    if not assessment.drift_check_enabled:
        raise PriceConfigurationError(
            "Stooq drift check is disabled until adjustment basis is empirically classified"
        )
    issues: list[StooqDriftIssue] = []
    for sample in samples:
        expected = (
            sample.split_only_close
            if assessment.basis == "splits_only"
            else sample.total_return_close
        )
        drift = _relative_error(sample.stooq_close, expected)
        if drift > DRIFT_TOLERANCE:
            issues.append(
                StooqDriftIssue(
                    sample.security_id,
                    sample.session,
                    drift,
                    sample.evidence_pointer,
                )
            )
    return tuple(issues)
