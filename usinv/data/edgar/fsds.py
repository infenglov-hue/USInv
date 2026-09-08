"""Lossless SEC FSDS ZIP-to-Parquet ingestion with point-in-time provenance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from importlib.metadata import version as package_version
from pathlib import Path, PurePosixPath
from typing import Any, Final
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.dataset as pads
import pyarrow.parquet as pq
from secfsdstools.a_utils.constants import PA_SCHEMA_MAP

from usinv.config import AppConfig
from usinv.data.edgar.bulk import (
    FsdsArchiveClient,
    FsdsArchiveError,
    FsdsArchiveRecord,
    FsdsQuarter,
    fsds_quarter_range,
)
from usinv.data.edgar.client import EdgarConfigurationError

SUPPORTED_SECFSDS_VERSION: Final = "2.4.3"
INGEST_SCHEMA_VERSION: Final = 1
INGEST_ADAPTER_VERSION: Final = "usinv-fsds-lossless-v1"
FSDS_ACCEPTANCE_TIMEZONE: Final = ZoneInfo("America/New_York")
_ADSH_PATTERN: Final = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_RAW_FILES: Final = ("sub.txt", "num.txt", "pre.txt", "tag.txt")
_TAG_COLUMNS: Final = frozenset(
    {"tag", "version", "custom", "abstract", "datatype", "iord", "crdr", "tlabel", "doc"}
)
_UPSTREAM_COLUMNS: Final = {
    file_name: frozenset(PA_SCHEMA_MAP[file_name].names)
    for file_name in ("sub.txt", "num.txt", "pre.txt")
}
EXPECTED_RAW_COLUMNS: Final = {**_UPSTREAM_COLUMNS, "tag.txt": _TAG_COLUMNS}
_EXPECTED_ARTIFACT_PATHS: Final = {
    "raw_sub": "raw/sub.parquet",
    "raw_num": "raw/num.parquet",
    "raw_pre": "raw/pre.parquet",
    "raw_tag": "raw/tag.parquet",
    "filings": "filings.parquet",
    "facts_raw": "facts_raw.parquet",
}


class FsdsIngestError(FsdsArchiveError):
    """Raised when archived FSDS data cannot be ingested reproducibly."""


class FsdsSchemaError(FsdsIngestError):
    """Raised when SEC rows violate the pinned FSDS field contract."""


@dataclass(frozen=True, slots=True)
class FsdsTableArtifact:
    """Hash and schema evidence for one generated Parquet table."""

    name: str
    relative_path: str
    row_count: int
    byte_count: int
    content_sha256: str
    arrow_schema: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "content_sha256": self.content_sha256,
            "arrow_schema": self.arrow_schema,
        }


@dataclass(frozen=True, slots=True)
class FsdsIngestResult:
    """One immutable source-version ingestion result."""

    quarter: FsdsQuarter
    source_sha256: str
    batch_id: str
    output_dir: Path
    created_at: datetime
    tables: tuple[FsdsTableArtifact, ...]
    from_cache: bool

    def table_path(self, name: str) -> Path:
        artifact = next((table for table in self.tables if table.name == name), None)
        if artifact is None:
            raise KeyError(name)
        return self.output_dir.joinpath(*PurePosixPath(artifact.relative_path).parts)


@dataclass(frozen=True, slots=True)
class _FilingContext:
    cik: int
    form: str
    period: date | None
    fy: int | None
    fp: str | None
    filed: date
    accepted: datetime
    prevrpt: bool


FILINGS_SCHEMA: Final = pa.schema(
    [
        ("batch_id", pa.string()),
        ("source_quarter", pa.string()),
        ("source_sha256", pa.string()),
        ("adsh", pa.string()),
        ("cik", pa.int64()),
        ("name", pa.string()),
        ("sic", pa.int32()),
        ("countryinc", pa.string()),
        ("former", pa.string()),
        ("changed", pa.date32()),
        ("afs", pa.string()),
        ("fye", pa.string()),
        ("form", pa.string()),
        ("period", pa.date32()),
        ("fy", pa.int32()),
        ("fp", pa.string()),
        ("filed", pa.date32()),
        ("accepted", pa.timestamp("us", tz="UTC")),
        ("accepted_raw", pa.string()),
        ("prevrpt", pa.bool_()),
        ("instance", pa.string()),
        ("nciks", pa.int16()),
        ("aciks", pa.string()),
    ]
)

FACTS_RAW_SCHEMA: Final = pa.schema(
    [
        ("batch_id", pa.string()),
        ("source_quarter", pa.string()),
        ("source_sha256", pa.string()),
        ("adsh", pa.string()),
        ("cik", pa.int64()),
        ("form", pa.string()),
        ("filing_period", pa.date32()),
        ("filing_fy", pa.int32()),
        ("filing_fp", pa.string()),
        ("filed", pa.date32()),
        ("accepted", pa.timestamp("us", tz="UTC")),
        ("filing_prevrpt", pa.bool_()),
        ("tag", pa.string()),
        ("version", pa.string()),
        ("ddate", pa.date32()),
        ("qtrs", pa.int16()),
        ("uom", pa.string()),
        ("segments", pa.string()),
        ("coreg", pa.string()),
        ("value", pa.decimal128(28, 4)),
        ("footnote", pa.string()),
        ("is_consolidated", pa.bool_()),
    ]
)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _required(row: dict[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FsdsSchemaError(f"FSDS field {field!r} is required")
    return value.strip()


def _optional(row: dict[str, object], field: str) -> str | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FsdsSchemaError(f"FSDS field {field!r} must be text")
    return value


def _integer(value: str | None, field: str, *, required: bool = False) -> int | None:
    if value is None or not value.strip():
        if required:
            raise FsdsSchemaError(f"FSDS field {field!r} is required")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FsdsSchemaError(f"FSDS field {field!r} is not an integer") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise FsdsSchemaError(f"FSDS field {field!r} is not an integer")
    return int(parsed)


def _date(value: str | None, field: str, *, required: bool = False) -> date | None:
    if value is None or not value.strip():
        if required:
            raise FsdsSchemaError(f"FSDS field {field!r} is required")
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise FsdsSchemaError(f"FSDS field {field!r} is not YYYYMMDD") from exc


def _boolean(value: str | None, field: str) -> bool:
    if value == "0":
        return False
    if value == "1":
        return True
    raise FsdsSchemaError(f"FSDS field {field!r} is not 0 or 1")


def _accepted(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FsdsSchemaError("FSDS accepted is not an ISO datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=FSDS_ACCEPTANCE_TIMEZONE)
    return parsed.astimezone(UTC)


def _decimal_value(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FsdsSchemaError("FSDS NUM value is not decimal") from exc
    if not parsed.is_finite():
        raise FsdsSchemaError("FSDS NUM value is not finite")
    return parsed


def read_consolidated_facts_as_of(
    facts_path: str | Path,
    as_of: datetime,
    *,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """Read only consolidated raw facts accepted by a timezone-aware cutoff."""
    if as_of.tzinfo is None:
        raise EdgarConfigurationError("FSDS facts as_of timestamp must be timezone-aware")
    dataset = pads.dataset(Path(facts_path), format="parquet")
    required = {"accepted", "is_consolidated"}
    if not required.issubset(dataset.schema.names):
        raise FsdsSchemaError("facts Parquet lacks availability or consolidation fields")
    cutoff = pa.scalar(as_of.astimezone(UTC), type=pa.timestamp("us", tz="UTC"))
    expression = (pads.field("accepted") <= cutoff) & pads.field("is_consolidated")
    return dataset.to_table(filter=expression, columns=list(columns) if columns else None)


class FsdsIngestor:
    """Convert one verified FSDS archive version into immutable Parquet artifacts."""

    def __init__(
        self,
        *,
        archive: FsdsArchiveClient,
        output_dir: str | Path,
        now: Callable[[], datetime] | None = None,
        block_size: int = 1024 * 1024,
        batch_size: int = 65_536,
    ) -> None:
        installed = package_version("secfsdstools")
        if installed != SUPPORTED_SECFSDS_VERSION:
            raise EdgarConfigurationError(
                f"secfsdstools {SUPPORTED_SECFSDS_VERSION} is required; found {installed}"
            )
        if block_size < 64 * 1024 or batch_size < 1:
            raise EdgarConfigurationError("FSDS ingestion block and batch sizes are invalid")
        self.archive = archive
        self.output_dir = Path(output_dir)
        self._now = now or (lambda: datetime.now(UTC))
        self.block_size = block_size
        self.batch_size = batch_size
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        archive_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> FsdsIngestor:
        archive = FsdsArchiveClient.from_config(config, archive_dir=archive_dir)
        settings = config.settings
        resolved_output = (
            Path(output_dir)
            if output_dir is not None
            else Path(settings.paths.data_dir) / "sec" / "fsds_parquet"
        )
        return cls(archive=archive, output_dir=resolved_output, **kwargs)

    @staticmethod
    def _batch_id(record: FsdsArchiveRecord) -> str:
        return f"sec_fsds:{record.quarter.label}:{record.content_sha256}"

    def _target_dir(self, record: FsdsArchiveRecord) -> Path:
        return self.output_dir / record.quarter.label / record.content_sha256

    @staticmethod
    def _zip_members(archive: zipfile.ZipFile) -> dict[str, str]:
        members = {name.casefold(): name for name in archive.namelist()}
        if not set(_RAW_FILES).issubset(members):
            raise FsdsSchemaError("FSDS ZIP no longer contains all four raw tables")
        return members

    @staticmethod
    def _header(archive: zipfile.ZipFile, member: str) -> tuple[str, ...]:
        with archive.open(member) as source:
            line = source.readline()
        try:
            decoded = line.decode("utf-8-sig").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise FsdsSchemaError(f"FSDS member {member!r} header is not UTF-8") from exc
        columns = tuple(decoded.split("\t")) if decoded else ()
        if len(columns) != len(set(columns)):
            raise FsdsSchemaError(f"FSDS member {member!r} contains duplicate columns")
        return columns

    def _write_raw_table(
        self,
        archive: zipfile.ZipFile,
        member: str,
        file_name: str,
        destination: Path,
    ) -> int:
        columns = self._header(archive, member)
        expected = EXPECTED_RAW_COLUMNS[file_name]
        if set(columns) != expected:
            missing = sorted(expected.difference(columns))
            extra = sorted(set(columns).difference(expected))
            raise FsdsSchemaError(
                f"FSDS {file_name} schema drift; missing={missing}, extra={extra}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        convert_options = pacsv.ConvertOptions(
            column_types={column: pa.string() for column in columns},
            null_values=[],
            strings_can_be_null=False,
        )
        read_options = pacsv.ReadOptions(block_size=self.block_size, use_threads=True)
        parse_options = pacsv.ParseOptions(delimiter="\t", newlines_in_values=True)
        row_count = 0
        try:
            with archive.open(member) as source:
                reader = pacsv.open_csv(
                    pa.PythonFile(source, mode="r"),
                    read_options=read_options,
                    parse_options=parse_options,
                    convert_options=convert_options,
                )
                with pq.ParquetWriter(
                    destination,
                    reader.schema,
                    compression="zstd",
                    version="2.6",
                    use_dictionary=True,
                ) as writer:
                    for batch in reader:
                        writer.write_batch(batch)
                        row_count += batch.num_rows
        except (OSError, pa.ArrowException) as exc:
            raise FsdsSchemaError(f"failed to convert raw FSDS member {file_name}") from exc
        return row_count

    @staticmethod
    def _filing_row(
        row: dict[str, object], record: FsdsArchiveRecord, batch_id: str
    ) -> tuple[dict[str, object], _FilingContext]:
        adsh = _required(row, "adsh")
        if not _ADSH_PATTERN.fullmatch(adsh):
            raise FsdsSchemaError(f"invalid FSDS accession number {adsh!r}")
        cik = _integer(_optional(row, "cik"), "cik", required=True)
        if cik is None or cik <= 0:
            raise FsdsSchemaError("FSDS cik must be positive")
        form = _required(row, "form")
        period = _date(_optional(row, "period"), "period")
        fy = _integer(_optional(row, "fy"), "fy")
        fp = _optional(row, "fp")
        filed = _date(_optional(row, "filed"), "filed", required=True)
        if filed is None:
            raise AssertionError("required filed date was not parsed")
        accepted_raw = _required(row, "accepted")
        accepted = _accepted(accepted_raw)
        prevrpt = _boolean(_optional(row, "prevrpt"), "prevrpt")
        output = {
            "batch_id": batch_id,
            "source_quarter": record.quarter.label,
            "source_sha256": record.content_sha256,
            "adsh": adsh,
            "cik": cik,
            "name": _required(row, "name"),
            "sic": _integer(_optional(row, "sic"), "sic"),
            "countryinc": _optional(row, "countryinc"),
            "former": _optional(row, "former"),
            "changed": _date(_optional(row, "changed"), "changed"),
            "afs": _optional(row, "afs"),
            "fye": _optional(row, "fye"),
            "form": form,
            "period": period,
            "fy": fy,
            "fp": fp,
            "filed": filed,
            "accepted": accepted,
            "accepted_raw": accepted_raw,
            "prevrpt": prevrpt,
            "instance": _required(row, "instance"),
            "nciks": _integer(_optional(row, "nciks"), "nciks"),
            "aciks": _optional(row, "aciks"),
        }
        context = _FilingContext(
            cik=cik,
            form=form,
            period=period,
            fy=fy,
            fp=fp,
            filed=filed,
            accepted=accepted,
            prevrpt=prevrpt,
        )
        return output, context

    def _write_filings(
        self,
        raw_sub_path: Path,
        destination: Path,
        record: FsdsArchiveRecord,
        batch_id: str,
    ) -> tuple[int, dict[str, _FilingContext]]:
        contexts: dict[str, _FilingContext] = {}
        row_count = 0
        with pq.ParquetWriter(destination, FILINGS_SCHEMA, compression="zstd") as writer:
            parquet = pq.ParquetFile(raw_sub_path)
            for batch in parquet.iter_batches(batch_size=self.batch_size):
                output_rows: list[dict[str, object]] = []
                for raw_row in batch.to_pylist():
                    output, context = self._filing_row(raw_row, record, batch_id)
                    adsh = str(output["adsh"])
                    if adsh in contexts:
                        raise FsdsSchemaError(f"duplicate FSDS submission {adsh}")
                    contexts[adsh] = context
                    output_rows.append(output)
                if output_rows:
                    writer.write_table(pa.Table.from_pylist(output_rows, schema=FILINGS_SCHEMA))
                    row_count += len(output_rows)
        return row_count, contexts

    @staticmethod
    def _fact_row(
        row: dict[str, object],
        filings: dict[str, _FilingContext],
        record: FsdsArchiveRecord,
        batch_id: str,
    ) -> dict[str, object]:
        adsh = _required(row, "adsh")
        filing = filings.get(adsh)
        if filing is None:
            raise FsdsSchemaError(f"FSDS NUM row references unknown submission {adsh}")
        coreg = _optional(row, "coreg")
        segments = _optional(row, "segments")
        ddate = _date(_optional(row, "ddate"), "ddate", required=True)
        qtrs = _integer(_optional(row, "qtrs"), "qtrs", required=True)
        if ddate is None or qtrs is None:
            raise AssertionError("required NUM date or quarter count was not parsed")
        return {
            "batch_id": batch_id,
            "source_quarter": record.quarter.label,
            "source_sha256": record.content_sha256,
            "adsh": adsh,
            "cik": filing.cik,
            "form": filing.form,
            "filing_period": filing.period,
            "filing_fy": filing.fy,
            "filing_fp": filing.fp,
            "filed": filing.filed,
            "accepted": filing.accepted,
            "filing_prevrpt": filing.prevrpt,
            "tag": _required(row, "tag"),
            "version": _required(row, "version"),
            "ddate": ddate,
            "qtrs": qtrs,
            "uom": _required(row, "uom"),
            "segments": segments,
            "coreg": coreg,
            "value": _decimal_value(_optional(row, "value")),
            "footnote": _optional(row, "footnote"),
            "is_consolidated": coreg is None and segments is None,
        }

    def _write_facts(
        self,
        raw_num_path: Path,
        destination: Path,
        record: FsdsArchiveRecord,
        batch_id: str,
        filings: dict[str, _FilingContext],
    ) -> tuple[int, int]:
        """Write NUM facts; SEC occasionally ships NUM rows whose adsh is absent
        from the same quarter's SUB table (known upstream inconsistency). Those
        rows have no submission context and are quarantined (counted, skipped)
        instead of failing the whole quarter."""
        row_count = 0
        orphan_count = 0
        try:
            with pq.ParquetWriter(destination, FACTS_RAW_SCHEMA, compression="zstd") as writer:
                parquet = pq.ParquetFile(raw_num_path)
                for batch in parquet.iter_batches(batch_size=self.batch_size):
                    output_rows = []
                    for row in batch.to_pylist():
                        adsh = row.get("adsh")
                        if filings.get(adsh if isinstance(adsh, str) else None) is None:
                            orphan_count += 1
                            continue
                        output_rows.append(self._fact_row(row, filings, record, batch_id))
                    if output_rows:
                        writer.write_table(
                            pa.Table.from_pylist(output_rows, schema=FACTS_RAW_SCHEMA)
                        )
                        row_count += len(output_rows)
        except (pa.ArrowException, InvalidOperation) as exc:
            raise FsdsSchemaError("FSDS NUM values violate NUMERIC(28,4)") from exc
        return row_count, orphan_count

    @staticmethod
    def _artifact(name: str, relative_path: str, root: Path) -> FsdsTableArtifact:
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        content_sha256, byte_count = _sha256_file(path)
        parquet = pq.ParquetFile(path)
        return FsdsTableArtifact(
            name=name,
            relative_path=relative_path,
            row_count=parquet.metadata.num_rows,
            byte_count=byte_count,
            content_sha256=content_sha256,
            arrow_schema=str(parquet.schema_arrow),
        )

    def _created_now(self) -> datetime:
        observed = self._now()
        if observed.tzinfo is None:
            raise FsdsIngestError("FSDS ingestion clock must be timezone-aware")
        return observed.astimezone(UTC)

    def _write_manifest(
        self,
        root: Path,
        record: FsdsArchiveRecord,
        created_at: datetime,
        artifacts: tuple[FsdsTableArtifact, ...],
        fact_orphans: int = 0,
    ) -> None:
        payload = {
            "schema_version": INGEST_SCHEMA_VERSION,
            "fact_rows_without_submission_quarantined": fact_orphans,
            "dataset": "sec_financial_statement_data_sets_parquet",
            "adapter_version": INGEST_ADAPTER_VERSION,
            "secfsdstools_version": SUPPORTED_SECFSDS_VERSION,
            "batch_id": self._batch_id(record),
            "created_at": created_at.isoformat(),
            "source": {
                "quarter": record.quarter.label,
                "content_sha256": record.content_sha256,
                "object_path": record.object_path,
                "archive_checked_at": record.checked_at.isoformat(),
            },
            "tables": [artifact.to_dict() for artifact in artifacts],
        }
        (root / "ingest_manifest.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _load_existing(self, target: Path, record: FsdsArchiveRecord) -> FsdsIngestResult:
        manifest_path = target / "ingest_manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            source = payload["source"]
            created_at = datetime.fromisoformat(payload["created_at"])
            table_values = payload["tables"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FsdsIngestError("FSDS ingest manifest is missing or invalid") from exc
        if (
            payload.get("schema_version") != INGEST_SCHEMA_VERSION
            or payload.get("dataset") != "sec_financial_statement_data_sets_parquet"
            or payload.get("adapter_version") != INGEST_ADAPTER_VERSION
            or payload.get("secfsdstools_version") != SUPPORTED_SECFSDS_VERSION
            or payload.get("batch_id") != self._batch_id(record)
            or not isinstance(source, dict)
            or source.get("quarter") != record.quarter.label
            or source.get("content_sha256") != record.content_sha256
            or source.get("object_path") != record.object_path
            or created_at.tzinfo is None
            or not isinstance(table_values, list)
            or len(table_values) != len(_EXPECTED_ARTIFACT_PATHS)
            or not all(isinstance(value, dict) for value in table_values)
        ):
            raise FsdsIngestError("FSDS ingest manifest contradicts its source")
        by_name = {
            value.get("name"): value
            for value in table_values
            if isinstance(value, dict) and isinstance(value.get("name"), str)
        }
        if set(by_name) != set(_EXPECTED_ARTIFACT_PATHS):
            raise FsdsIngestError("FSDS ingest manifest has an unexpected table set")
        artifacts: list[FsdsTableArtifact] = []
        for name, relative_path in _EXPECTED_ARTIFACT_PATHS.items():
            value = by_name[name]
            if value.get("relative_path") != relative_path:
                raise FsdsIngestError("FSDS ingest manifest contains an unsafe table path")
            path = target.joinpath(*PurePosixPath(relative_path).parts)
            if not path.is_file():
                raise FsdsIngestError(f"FSDS ingest artifact is missing: {name}")
            actual_hash, actual_bytes = _sha256_file(path)
            try:
                parquet = pq.ParquetFile(path)
            except (OSError, pa.ArrowException) as exc:
                raise FsdsIngestError(f"FSDS ingest artifact verification failed: {name}") from exc
            if (
                value.get("content_sha256") != actual_hash
                or value.get("byte_count") != actual_bytes
                or value.get("row_count") != parquet.metadata.num_rows
                or value.get("arrow_schema") != str(parquet.schema_arrow)
            ):
                raise FsdsIngestError(f"FSDS ingest artifact verification failed: {name}")
            artifacts.append(
                FsdsTableArtifact(
                    name=name,
                    relative_path=relative_path,
                    row_count=parquet.metadata.num_rows,
                    byte_count=actual_bytes,
                    content_sha256=actual_hash,
                    arrow_schema=str(parquet.schema_arrow),
                )
            )
        return FsdsIngestResult(
            quarter=record.quarter,
            source_sha256=record.content_sha256,
            batch_id=self._batch_id(record),
            output_dir=target,
            created_at=created_at.astimezone(UTC),
            tables=tuple(artifacts),
            from_cache=True,
        )

    def ingest_record(self, record: FsdsArchiveRecord) -> FsdsIngestResult:
        """Ingest one exact archive record, preserving reprocessed versions separately."""
        with self._lock:
            source_path = self.archive.verify_record(record)
            target = self._target_dir(record)
            if target.exists():
                return self._load_existing(target, record)

            self.output_dir.mkdir(parents=True, exist_ok=True)
            staging = self.output_dir / f".ingest.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            staging.mkdir()
            try:
                with zipfile.ZipFile(source_path) as archive:
                    members = self._zip_members(archive)
                    for file_name in _RAW_FILES:
                        relative = _EXPECTED_ARTIFACT_PATHS[f"raw_{file_name[:-4]}"]
                        self._write_raw_table(
                            archive,
                            members[file_name],
                            file_name,
                            staging.joinpath(*PurePosixPath(relative).parts),
                        )
                batch_id = self._batch_id(record)
                _, filings = self._write_filings(
                    staging / "raw" / "sub.parquet",
                    staging / "filings.parquet",
                    record,
                    batch_id,
                )
                _fact_rows, fact_orphans = self._write_facts(
                    staging / "raw" / "num.parquet",
                    staging / "facts_raw.parquet",
                    record,
                    batch_id,
                    filings,
                )
                artifacts = tuple(
                    self._artifact(name, relative_path, staging)
                    for name, relative_path in _EXPECTED_ARTIFACT_PATHS.items()
                )
                created_at = self._created_now()
                self._write_manifest(staging, record, created_at, artifacts, fact_orphans)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(staging)
                    return self._load_existing(target, record)
                staging.replace(target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            return FsdsIngestResult(
                quarter=record.quarter,
                source_sha256=record.content_sha256,
                batch_id=batch_id,
                output_dir=target,
                created_at=created_at,
                tables=artifacts,
                from_cache=False,
            )

    def ingest_quarter(
        self,
        quarter: FsdsQuarter | str,
        *,
        archive_as_of: datetime | None = None,
    ) -> FsdsIngestResult:
        resolved = FsdsQuarter.parse(quarter) if isinstance(quarter, str) else quarter
        record = (
            self.archive.record_as_of(resolved, archive_as_of)
            if archive_as_of is not None
            else self.archive.latest(resolved)
        )
        if record is None:
            raise FsdsIngestError(f"no archived FSDS version is available for {resolved}")
        return self.ingest_record(record)

    def ingest_range(
        self,
        start: FsdsQuarter | str,
        end: FsdsQuarter | str,
        *,
        archive_as_of: datetime | None = None,
    ) -> tuple[FsdsIngestResult, ...]:
        return tuple(
            self.ingest_quarter(quarter, archive_as_of=archive_as_of)
            for quarter in fsds_quarter_range(start, end)
        )
