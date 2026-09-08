"""Immutable as-first-filed and latest SEC fact snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

import duckdb
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq

from usinv.config import AppConfig
from usinv.data.edgar.client import EdgarConfigurationError, EdgarError
from usinv.data.edgar.fsds import FACTS_RAW_SCHEMA, FsdsIngestResult

PIT_STORE_SCHEMA_VERSION: Final = 2
PIT_STORE_BUILDER_VERSION: Final = "usinv-pit-store-v3"
SUPPORTED_DUCKDB_VERSION: Final = "1.5.4"
PIT_KEY: Final = ("cik", "tag", "ddate", "qtrs", "uom")
_HASH_PATTERN: Final = "0123456789abcdef"
_VIEW_METADATA_KEY: Final = b"usinv.fact_view"
_EXPECTED_ARTIFACT_PATHS: Final = {
    "facts_pit": "facts_pit.parquet",
    "facts_latest": "facts_latest.parquet",
    "facts_conflicts": "facts_conflicts.parquet",
}
_SELECTION_CONTRACT: Final = {
    "facts_pit": "minimum accepted",
    "facts_latest": "maximum accepted",
    "equal_time_conflicts": "quarantine_key_and_count",
}

_FACT_VIEW_FIELDS: Final = [
    pa.field("cik", pa.int64()),
    pa.field("tag", pa.string()),
    pa.field("ddate", pa.date32()),
    pa.field("qtrs", pa.int16()),
    pa.field("uom", pa.string()),
    pa.field("value", pa.decimal128(28, 4)),
    pa.field("accepted", pa.timestamp("us", tz="UTC")),
    pa.field("adsh", pa.string()),
    pa.field("version", pa.string()),
    pa.field("form", pa.string()),
    pa.field("filed", pa.date32()),
    pa.field("filing_period", pa.date32()),
    pa.field("filing_fy", pa.int32()),
    pa.field("filing_fp", pa.string()),
    pa.field("footnote", pa.string()),
    pa.field("batch_id", pa.string()),
    pa.field("source_quarter", pa.string()),
    pa.field("source_sha256", pa.string()),
]
PIT_FACT_SCHEMA: Final = pa.schema(_FACT_VIEW_FIELDS, metadata={_VIEW_METADATA_KEY: b"facts_pit"})
LATEST_FACT_SCHEMA: Final = pa.schema(
    _FACT_VIEW_FIELDS, metadata={_VIEW_METADATA_KEY: b"facts_latest"}
)
PIT_CONFLICT_SCHEMA: Final = pa.schema(
    [
        pa.field("cik", pa.int64()),
        pa.field("tag", pa.string()),
        pa.field("ddate", pa.date32()),
        pa.field("qtrs", pa.int16()),
        pa.field("uom", pa.string()),
        pa.field("accepted", pa.timestamp("us", tz="UTC")),
        pa.field("distinct_values", pa.int64()),
        pa.field("candidate_rows", pa.int64()),
        pa.field("adshs", pa.list_(pa.field("element", pa.string()))),
        pa.field("values", pa.list_(pa.field("element", pa.string()))),
    ],
    metadata={_VIEW_METADATA_KEY: b"facts_conflicts"},
)
_FACT_COLUMNS_SQL: Final = ", ".join(field.name for field in _FACT_VIEW_FIELDS)
_KEY_SQL: Final = ", ".join(PIT_KEY)
_LOGICAL_VALUE_SQL: Final = "COALESCE(CAST(value AS VARCHAR), '<NULL>')"


class PitStoreError(EdgarError):
    """Raised when a PIT snapshot cannot be built or verified."""


class PitStoreConflictError(PitStoreError):
    """Raised when equal-time facts make the PIT key ambiguous."""


@dataclass(frozen=True, slots=True)
class PitInputBatch:
    """One hash-verified normalized fact batch used by a PIT snapshot."""

    batch_id: str
    source_quarter: str
    source_sha256: str
    facts_path: Path
    facts_content_sha256: str
    byte_count: int
    row_count: int
    arrow_schema: str

    @classmethod
    def from_fsds_result(cls, result: FsdsIngestResult) -> PitInputBatch:
        artifact = next((table for table in result.tables if table.name == "facts_raw"), None)
        if artifact is None:
            raise PitStoreError("FSDS ingest result has no facts_raw artifact")
        return cls(
            batch_id=result.batch_id,
            source_quarter=result.quarter.label,
            source_sha256=result.source_sha256,
            facts_path=result.table_path("facts_raw"),
            facts_content_sha256=artifact.content_sha256,
            byte_count=artifact.byte_count,
            row_count=artifact.row_count,
            arrow_schema=artifact.arrow_schema,
        )

    def manifest_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "source_quarter": self.source_quarter,
            "source_sha256": self.source_sha256,
            "facts_content_sha256": self.facts_content_sha256,
            "byte_count": self.byte_count,
            "row_count": self.row_count,
            "arrow_schema": self.arrow_schema,
        }


@dataclass(frozen=True, slots=True)
class PitTableArtifact:
    """Hash, row count and schema evidence for one fact view."""

    name: str
    relative_path: str
    row_count: int
    byte_count: int
    content_sha256: str
    arrow_schema: str

    def manifest_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "content_sha256": self.content_sha256,
            "arrow_schema": self.arrow_schema,
        }


@dataclass(frozen=True, slots=True)
class PitStoreResult:
    """One immutable content-addressed PIT/latest snapshot."""

    snapshot_id: str
    output_dir: Path
    created_at: datetime
    inputs: tuple[PitInputBatch, ...]
    tables: tuple[PitTableArtifact, ...]
    from_cache: bool

    def table_path(self, name: str) -> Path:
        artifact = next((table for table in self.tables if table.name == name), None)
        if artifact is None:
            raise KeyError(name)
        return self.output_dir.joinpath(*PurePosixPath(artifact.relative_path).parts)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in _HASH_PATTERN for character in value)


def _canonical_inputs(inputs: Sequence[PitInputBatch]) -> tuple[PitInputBatch, ...]:
    ordered = tuple(sorted(inputs, key=lambda item: (item.batch_id, item.facts_content_sha256)))
    if not ordered:
        raise PitStoreError("at least one normalized fact batch is required")
    if len({item.batch_id for item in ordered}) != len(ordered):
        raise PitStoreError("PIT input batch IDs must be unique")
    return ordered


def _snapshot_id(inputs: Sequence[PitInputBatch]) -> str:
    payload = {
        "schema_version": PIT_STORE_SCHEMA_VERSION,
        "builder_version": PIT_STORE_BUILDER_VERSION,
        "duckdb_version": SUPPORTED_DUCKDB_VERSION,
        "pit_key": list(PIT_KEY),
        "inputs": [item.manifest_dict() for item in inputs],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_pit_facts_as_of(
    facts_path: str | Path,
    as_of: datetime,
    *,
    columns: Sequence[str] | None = None,
) -> pa.Table:
    """Read only first-filed facts available by a timezone-aware cutoff."""
    if as_of.tzinfo is None:
        raise EdgarConfigurationError("PIT facts as_of timestamp must be timezone-aware")
    dataset = pads.dataset(Path(facts_path), format="parquet")
    if not dataset.schema.equals(PIT_FACT_SCHEMA, check_metadata=True):
        raise PitStoreError("safe PIT reader requires a verified facts_pit artifact")
    requested = list(columns) if columns is not None else None
    if requested is not None and not set(requested).issubset(PIT_FACT_SCHEMA.names):
        raise PitStoreError("PIT reader requested an unknown column")
    cutoff = pa.scalar(as_of.astimezone(UTC), type=pa.timestamp("us", tz="UTC"))
    return dataset.to_table(filter=pads.field("accepted") <= cutoff, columns=requested)


class PitStoreBuilder:
    """Build immutable PIT/latest Parquet snapshots from normalized fact batches."""

    def __init__(
        self,
        *,
        output_dir: str | Path,
        now: Callable[[], datetime] | None = None,
        batch_size: int = 65_536,
    ) -> None:
        if duckdb.__version__ != SUPPORTED_DUCKDB_VERSION:
            raise EdgarConfigurationError(
                f"duckdb {SUPPORTED_DUCKDB_VERSION} is required; found {duckdb.__version__}"
            )
        if batch_size < 1:
            raise EdgarConfigurationError("PIT store batch size must be positive")
        self.output_dir = Path(output_dir)
        self._now = now or (lambda: datetime.now(UTC))
        self.batch_size = batch_size
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        output_dir: str | Path | None = None,
        **kwargs: object,
    ) -> PitStoreBuilder:
        resolved = (
            Path(output_dir)
            if output_dir is not None
            else Path(config.settings.paths.data_dir) / "sec" / "pit_store"
        )
        return cls(output_dir=resolved, **kwargs)

    def _created_now(self) -> datetime:
        created_at = self._now()
        if created_at.tzinfo is None:
            raise PitStoreError("PIT store clock must be timezone-aware")
        return created_at.astimezone(UTC)

    @staticmethod
    def _verify_input(item: PitInputBatch) -> None:
        if (
            not item.batch_id
            or not item.source_quarter
            or not _is_sha256(item.source_sha256)
            or not _is_sha256(item.facts_content_sha256)
            or item.byte_count < 0
            or item.row_count < 0
            or not item.facts_path.is_file()
        ):
            raise PitStoreError("PIT input descriptor is invalid")
        actual_hash, actual_bytes = _sha256_file(item.facts_path)
        if actual_hash != item.facts_content_sha256 or actual_bytes != item.byte_count:
            raise PitStoreError(f"PIT input hash verification failed: {item.batch_id}")
        try:
            parquet = pq.ParquetFile(item.facts_path)
        except (OSError, pa.ArrowException) as exc:
            raise PitStoreError(f"PIT input Parquet is invalid: {item.batch_id}") from exc
        if (
            parquet.metadata.num_rows != item.row_count
            or str(parquet.schema_arrow) != item.arrow_schema
            or not parquet.schema_arrow.equals(FACTS_RAW_SCHEMA, check_metadata=True)
        ):
            raise PitStoreError(f"PIT input schema verification failed: {item.batch_id}")

    @staticmethod
    def _artifact(name: str, relative_path: str, root: Path) -> PitTableArtifact:
        path = root.joinpath(*PurePosixPath(relative_path).parts)
        content_sha256, byte_count = _sha256_file(path)
        parquet = pq.ParquetFile(path)
        return PitTableArtifact(
            name=name,
            relative_path=relative_path,
            row_count=parquet.metadata.num_rows,
            byte_count=byte_count,
            content_sha256=content_sha256,
            arrow_schema=str(parquet.schema_arrow),
        )

    def _write_view(
        self,
        connection: duckdb.DuckDBPyConnection,
        destination: Path,
        *,
        ascending: bool,
        schema: pa.Schema,
    ) -> int:
        direction = "ASC" if ascending else "DESC"
        query = f"""
            SELECT {_FACT_COLUMNS_SQL}
            FROM eligible_candidates
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY {_KEY_SQL}
                ORDER BY accepted {direction}, adsh {direction}, version {direction},
                         source_quarter ASC, source_sha256 ASC, batch_id ASC
            ) = 1
            ORDER BY {_KEY_SQL}, accepted, adsh, version
        """
        reader = connection.execute(query).to_arrow_reader(self.batch_size)
        row_count = 0
        with pq.ParquetWriter(
            destination,
            schema,
            compression="zstd",
            version="2.6",
            use_dictionary=True,
        ) as writer:
            for batch in reader:
                table = (
                    pa.Table.from_batches([batch])
                    .cast(schema)
                    .replace_schema_metadata(schema.metadata)
                )
                writer.write_table(table)
                row_count += table.num_rows
        return row_count

    def _write_conflicts(
        self,
        connection: duckdb.DuckDBPyConnection,
        destination: Path,
    ) -> int:
        # Join against the precomputed conflict_keys table instead of scanning all
        # 96M candidate rows: the list() aggregation buffers per group and OOMs on
        # the full table, while the conflicting-key join filters to a few hundred.
        query = f"""
            WITH conflicting_rows AS (
                SELECT candidates.*
                FROM candidates
                SEMI JOIN conflict_keys
                    ON candidates.cik = conflict_keys.cik
                   AND candidates.tag = conflict_keys.tag
                   AND candidates.ddate = conflict_keys.ddate
                   AND candidates.qtrs = conflict_keys.qtrs
                   AND candidates.uom = conflict_keys.uom
            )
            SELECT {_KEY_SQL}, accepted,
                   COUNT(DISTINCT {_LOGICAL_VALUE_SQL})::BIGINT AS distinct_values,
                   COUNT(*)::BIGINT AS candidate_rows,
                   list_sort(list_distinct(list(adsh))) AS adshs,
                   list_sort(list_distinct(list({_LOGICAL_VALUE_SQL}))) AS values
            FROM conflicting_rows
            GROUP BY {_KEY_SQL}, accepted
            HAVING COUNT(DISTINCT {_LOGICAL_VALUE_SQL}) > 1
            ORDER BY {_KEY_SQL}, accepted
        """
        reader = connection.execute(query).to_arrow_reader(self.batch_size)
        row_count = 0
        with pq.ParquetWriter(
            destination,
            PIT_CONFLICT_SCHEMA,
            compression="zstd",
            version="2.6",
            use_dictionary=True,
        ) as writer:
            for batch in reader:
                table = (
                    pa.Table.from_batches([batch])
                    .cast(PIT_CONFLICT_SCHEMA)
                    .replace_schema_metadata(PIT_CONFLICT_SCHEMA.metadata)
                )
                writer.write_table(table)
                row_count += table.num_rows
        return row_count

    @staticmethod
    def _assert_candidates(connection: duckdb.DuckDBPyConnection) -> None:
        invalid = connection.execute(
            """
            SELECT COUNT(*)
            FROM raw_facts
            WHERE cik IS NULL OR tag IS NULL OR ddate IS NULL OR qtrs IS NULL
               OR uom IS NULL OR accepted IS NULL OR adsh IS NULL
               OR batch_id IS NULL OR source_quarter IS NULL OR source_sha256 IS NULL
               OR is_consolidated IS DISTINCT FROM (coreg IS NULL AND segments IS NULL)
               OR NOT EXISTS (
                   SELECT 1
                   FROM declared_inputs
                   WHERE declared_inputs.batch_id = raw_facts.batch_id
                     AND declared_inputs.source_quarter = raw_facts.source_quarter
                     AND declared_inputs.source_sha256 = raw_facts.source_sha256
               )
            """
        ).fetchone()
        if invalid is None or invalid[0] != 0:
            raise PitStoreError("normalized facts violate PIT key or consolidation invariants")

    def _materialize(self, root: Path, inputs: Sequence[PitInputBatch]) -> None:
        temporary = root / "duckdb-tmp"
        temporary.mkdir()
        # A disk-backed database is required: the PIT window queries over ~96M
        # candidate rows cannot complete within a 15 GB in-memory limit even
        # with spilling enabled (verified: OOM at 7.4 GiB). The .duckdb file
        # lives under `temporary`, which is removed in the finally block.
        connection = duckdb.connect(database=str(temporary / "work.duckdb"))
        try:
            connection.execute("SET preserve_insertion_order = false")
            connection.execute("SET memory_limit = '10GB'")
            connection.execute("SET threads = 2")
            connection.register(
                "declared_inputs",
                pa.table(
                    {
                        "batch_id": [item.batch_id for item in inputs],
                        "source_quarter": [item.source_quarter for item in inputs],
                        "source_sha256": [item.source_sha256 for item in inputs],
                    }
                ),
            )
            # Persist intermediate sets as physical tables: temp views re-execute
            # the full 96M-row scan per reference and the window queries OOM even
            # on a disk-backed database. Tables spill to the .duckdb file instead.
            connection.from_parquet([str(item.facts_path) for item in inputs]).create_view(
                "raw_facts"
            )
            connection.execute(
                f"""
                CREATE TABLE candidates AS
                SELECT {_FACT_COLUMNS_SQL}
                FROM raw_facts
                WHERE (is_consolidated IS TRUE AND coreg IS NULL AND segments IS NULL)
                   OR (
                       coreg IS NULL
                       AND segments IN (
                           'FreshStartAdjustmentsTypeOfFreshStartAdjustment=Successor;',
                           'BusinessSegments=HomebuildingSegment;ConsolidationItems=OperatingSegments;SubsegmentsConsolidationItems=ReportableSubsegments;',
                           'ConsolidationItems=OperatingSegments;'
                       )
                       AND NOT EXISTS (
                           SELECT 1
                           FROM raw_facts rf
                           WHERE rf.cik = raw_facts.cik
                             AND rf.tag = raw_facts.tag
                             AND rf.ddate = raw_facts.ddate
                             AND rf.qtrs = raw_facts.qtrs
                             AND rf.uom = raw_facts.uom
                             AND rf.coreg IS NULL
                             AND rf.segments IS NULL
                       )
                   )
                """
            )
            self._assert_candidates(connection)
            connection.execute(
                f"""
                CREATE TABLE conflict_keys AS
                SELECT DISTINCT {_KEY_SQL}
                FROM (
                    SELECT {_KEY_SQL}, accepted
                    FROM candidates
                    GROUP BY {_KEY_SQL}, accepted
                    HAVING COUNT(DISTINCT {_LOGICAL_VALUE_SQL}) > 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE eligible_candidates AS
                SELECT candidates.*
                FROM candidates
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM conflict_keys
                    WHERE conflict_keys.cik = candidates.cik
                      AND conflict_keys.tag = candidates.tag
                      AND conflict_keys.ddate = candidates.ddate
                      AND conflict_keys.qtrs = candidates.qtrs
                      AND conflict_keys.uom = candidates.uom
                )
                """
            )
            self._write_view(
                connection,
                root / "facts_pit.parquet",
                ascending=True,
                schema=PIT_FACT_SCHEMA,
            )
            self._write_view(
                connection,
                root / "facts_latest.parquet",
                ascending=False,
                schema=LATEST_FACT_SCHEMA,
            )
            self._write_conflicts(connection, root / "facts_conflicts.parquet")
        except duckdb.Error as exc:
            import sys as _sys

            print(f"pit_materialize_failed: {type(exc).__name__}: {exc}", file=_sys.stderr)
            raise PitStoreError("DuckDB could not materialize PIT fact views") from exc
        finally:
            connection.close()
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _write_manifest(
        root: Path,
        *,
        snapshot_id: str,
        created_at: datetime,
        inputs: Sequence[PitInputBatch],
        tables: Sequence[PitTableArtifact],
    ) -> None:
        payload = {
            "schema_version": PIT_STORE_SCHEMA_VERSION,
            "dataset": "sec_point_in_time_fact_views",
            "builder_version": PIT_STORE_BUILDER_VERSION,
            "duckdb_version": SUPPORTED_DUCKDB_VERSION,
            "snapshot_id": snapshot_id,
            "created_at": created_at.isoformat(),
            "pit_key": list(PIT_KEY),
            "selection": _SELECTION_CONTRACT,
            "inputs": [item.manifest_dict() for item in inputs],
            "tables": [table.manifest_dict() for table in tables],
        }
        (root / "pit_manifest.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _load_existing(
        self,
        target: Path,
        *,
        snapshot_id: str,
        inputs: tuple[PitInputBatch, ...],
    ) -> PitStoreResult:
        try:
            payload = json.loads((target / "pit_manifest.json").read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(payload["created_at"])
            table_values = payload["tables"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PitStoreError("PIT snapshot manifest is missing or invalid") from exc
        expected_inputs = [item.manifest_dict() for item in inputs]
        if (
            payload.get("schema_version") != PIT_STORE_SCHEMA_VERSION
            or payload.get("dataset") != "sec_point_in_time_fact_views"
            or payload.get("builder_version") != PIT_STORE_BUILDER_VERSION
            or payload.get("duckdb_version") != SUPPORTED_DUCKDB_VERSION
            or payload.get("snapshot_id") != snapshot_id
            or payload.get("pit_key") != list(PIT_KEY)
            or payload.get("selection") != _SELECTION_CONTRACT
            or payload.get("inputs") != expected_inputs
            or created_at.tzinfo is None
            or not isinstance(table_values, list)
            or len(table_values) != len(_EXPECTED_ARTIFACT_PATHS)
            or not all(isinstance(value, dict) for value in table_values)
        ):
            raise PitStoreError("PIT snapshot manifest contradicts its inputs")
        by_name = {value.get("name"): value for value in table_values}
        if set(by_name) != set(_EXPECTED_ARTIFACT_PATHS):
            raise PitStoreError("PIT snapshot manifest has an unexpected table set")
        tables: list[PitTableArtifact] = []
        for name, relative_path in _EXPECTED_ARTIFACT_PATHS.items():
            value = by_name[name]
            if value.get("relative_path") != relative_path:
                raise PitStoreError("PIT snapshot manifest contains an unsafe table path")
            path = target.joinpath(*PurePosixPath(relative_path).parts)
            if not path.is_file():
                raise PitStoreError(f"PIT snapshot artifact is missing: {name}")
            actual_hash, actual_bytes = _sha256_file(path)
            try:
                parquet = pq.ParquetFile(path)
            except (OSError, pa.ArrowException) as exc:
                raise PitStoreError(f"PIT snapshot verification failed: {name}") from exc
            expected_schema = {
                "facts_pit": PIT_FACT_SCHEMA,
                "facts_latest": LATEST_FACT_SCHEMA,
                "facts_conflicts": PIT_CONFLICT_SCHEMA,
            }[name]
            if (
                value.get("content_sha256") != actual_hash
                or value.get("byte_count") != actual_bytes
                or value.get("row_count") != parquet.metadata.num_rows
                or value.get("arrow_schema") != str(parquet.schema_arrow)
                or not parquet.schema_arrow.equals(expected_schema, check_metadata=True)
            ):
                raise PitStoreError(f"PIT snapshot verification failed: {name}")
            tables.append(
                PitTableArtifact(
                    name=name,
                    relative_path=relative_path,
                    row_count=parquet.metadata.num_rows,
                    byte_count=actual_bytes,
                    content_sha256=actual_hash,
                    arrow_schema=str(parquet.schema_arrow),
                )
            )
        return PitStoreResult(
            snapshot_id=snapshot_id,
            output_dir=target,
            created_at=created_at.astimezone(UTC),
            inputs=inputs,
            tables=tuple(tables),
            from_cache=True,
        )

    def build(self, inputs: Sequence[PitInputBatch]) -> PitStoreResult:
        """Build or verify the immutable snapshot selected by exact input hashes."""
        canonical = _canonical_inputs(inputs)
        for item in canonical:
            self._verify_input(item)
        snapshot_id = _snapshot_id(canonical)
        target = self.output_dir / "snapshots" / snapshot_id
        with self._lock:
            if target.exists():
                return self._load_existing(target, snapshot_id=snapshot_id, inputs=canonical)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            staging = self.output_dir / f".pit.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            staging.mkdir()
            try:
                self._materialize(staging, canonical)
                tables = tuple(
                    self._artifact(name, relative_path, staging)
                    for name, relative_path in _EXPECTED_ARTIFACT_PATHS.items()
                )
                created_at = self._created_now()
                self._write_manifest(
                    staging,
                    snapshot_id=snapshot_id,
                    created_at=created_at,
                    inputs=canonical,
                    tables=tables,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(staging)
                    return self._load_existing(target, snapshot_id=snapshot_id, inputs=canonical)
                staging.replace(target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            return PitStoreResult(
                snapshot_id=snapshot_id,
                output_dir=target,
                created_at=created_at,
                inputs=canonical,
                tables=tables,
                from_cache=False,
            )
