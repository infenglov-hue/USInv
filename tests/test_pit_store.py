from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import usinv.data.edgar.pit_store as pit_store_module
from usinv.data.edgar.client import EdgarConfigurationError
from usinv.data.edgar.fsds import FACTS_RAW_SCHEMA
from usinv.data.edgar.pit_store import (
    LATEST_FACT_SCHEMA,
    PIT_CONFLICT_SCHEMA,
    PIT_FACT_SCHEMA,
    PitInputBatch,
    PitStoreBuilder,
    PitStoreError,
    read_pit_facts_as_of,
)


def _fact(
    *,
    batch_id: str,
    source_quarter: str,
    source_sha256: str,
    adsh: str,
    accepted: datetime,
    value: str,
    ddate: date = date(2026, 3, 31),
    tag: str = "Revenue",
    is_consolidated: bool = True,
    coreg: str | None = None,
    segments: str | None = None,
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "source_quarter": source_quarter,
        "source_sha256": source_sha256,
        "adsh": adsh,
        "cik": 1234567,
        "form": "10-Q/A" if adsh.endswith("2") else "10-Q",
        "filing_period": ddate,
        "filing_fy": 2026,
        "filing_fp": "Q1",
        "filed": accepted.date(),
        "accepted": accepted,
        "filing_prevrpt": adsh.endswith("1"),
        "tag": tag,
        "version": "us-gaap/2026",
        "ddate": ddate,
        "qtrs": 1,
        "uom": "USD",
        "segments": segments,
        "coreg": coreg,
        "value": Decimal(value),
        "footnote": None,
        "is_consolidated": is_consolidated,
    }


def _batch(
    tmp_path: Path,
    name: str,
    rows: list[dict[str, object]],
    *,
    source_sha256: str,
) -> PitInputBatch:
    path = tmp_path / f"{name}.parquet"
    table = pa.Table.from_pylist(rows, schema=FACTS_RAW_SCHEMA)
    pq.write_table(table, path, compression="zstd")
    payload = path.read_bytes()
    return PitInputBatch(
        batch_id=f"batch:{name}",
        source_quarter="2026q2",
        source_sha256=source_sha256,
        facts_path=path,
        facts_content_sha256=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
        row_count=table.num_rows,
        arrow_schema=str(table.schema),
    )


def _base_and_amendment(tmp_path: Path) -> tuple[PitInputBatch, PitInputBatch]:
    base_sha = "1" * 64
    amendment_sha = "2" * 64
    base = _batch(
        tmp_path,
        "base",
        [
            _fact(
                batch_id="batch:base",
                source_quarter="2026q2",
                source_sha256=base_sha,
                adsh="0001234567-26-000001",
                accepted=datetime(2026, 5, 1, 20, tzinfo=UTC),
                value="100.0000",
            )
        ],
        source_sha256=base_sha,
    )
    amendment = _batch(
        tmp_path,
        "amendment",
        [
            _fact(
                batch_id="batch:amendment",
                source_quarter="2026q2",
                source_sha256=amendment_sha,
                adsh="0001234567-26-000002",
                accepted=datetime(2026, 5, 8, 21, 31, tzinfo=UTC),
                value="130.0000",
            ),
            _fact(
                batch_id="batch:amendment",
                source_quarter="2026q2",
                source_sha256=amendment_sha,
                adsh="0001234567-26-000003",
                accepted=datetime(2026, 6, 1, 20, tzinfo=UTC),
                value="75.0000",
                ddate=date(2026, 6, 30),
                tag="NetIncomeLoss",
            ),
        ],
        source_sha256=amendment_sha,
    )
    return base, amendment


def test_amendment_is_latest_but_never_mutates_first_file_and_future_is_invisible(
    tmp_path: Path,
) -> None:
    base, amendment = _base_and_amendment(tmp_path)
    result = PitStoreBuilder(output_dir=tmp_path / "store").build([base, amendment])

    pit = pq.read_table(result.table_path("facts_pit"))
    latest = pq.read_table(result.table_path("facts_latest"))
    assert pit.schema.equals(PIT_FACT_SCHEMA, check_metadata=True)
    assert latest.schema.equals(LATEST_FACT_SCHEMA, check_metadata=True)
    assert pit.num_rows == 2
    assert latest.num_rows == 2
    revenue_pit = next(row for row in pit.to_pylist() if row["tag"] == "Revenue")
    revenue_latest = next(row for row in latest.to_pylist() if row["tag"] == "Revenue")
    assert revenue_pit["value"] == Decimal("100.0000")
    assert revenue_pit["adsh"] == "0001234567-26-000001"
    assert revenue_latest["value"] == Decimal("130.0000")
    assert revenue_latest["adsh"] == "0001234567-26-000002"

    early = read_pit_facts_as_of(
        result.table_path("facts_pit"),
        datetime(2026, 5, 2, tzinfo=UTC),
        columns=["tag", "value", "accepted"],
    )
    assert early.to_pylist() == [
        {
            "tag": "Revenue",
            "value": Decimal("100.0000"),
            "accepted": datetime(2026, 5, 1, 20, tzinfo=UTC),
        }
    ]
    with pytest.raises(PitStoreError, match="facts_pit"):
        read_pit_facts_as_of(result.table_path("facts_latest"), datetime(2026, 7, 1, tzinfo=UTC))


def test_snapshots_are_immutable_idempotent_and_input_order_independent(tmp_path: Path) -> None:
    base, amendment = _base_and_amendment(tmp_path)
    builder = PitStoreBuilder(
        output_dir=tmp_path / "store",
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )
    original = builder.build([base])
    original_bytes = original.table_path("facts_pit").read_bytes()

    amended = builder.build([base, amendment])
    cached = builder.build([amendment, base])

    assert original.snapshot_id != amended.snapshot_id
    assert original.table_path("facts_pit").read_bytes() == original_bytes
    assert original.tables[0].name == amended.tables[0].name == "facts_pit"
    assert original.tables[1].content_sha256 != amended.tables[1].content_sha256
    amended_revenue = next(
        row
        for row in pq.read_table(amended.table_path("facts_pit")).to_pylist()
        if row["tag"] == "Revenue"
    )
    assert amended_revenue["value"] == Decimal("100.0000")
    assert amended_revenue["adsh"] == "0001234567-26-000001"
    assert pq.read_table(original.table_path("facts_latest"))["value"][0].as_py() == Decimal(
        "100.0000"
    )
    assert cached.from_cache and cached.snapshot_id == amended.snapshot_id
    assert cached.created_at == datetime(2026, 7, 18, tzinfo=UTC)


def test_equal_time_conflicting_facts_are_quarantined_and_counted(tmp_path: Path) -> None:
    source_sha = "3" * 64
    accepted = datetime(2026, 5, 1, 20, tzinfo=UTC)
    conflict = _batch(
        tmp_path,
        "conflict",
        [
            _fact(
                batch_id="batch:conflict",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000000",
                accepted=datetime(2026, 4, 1, 20, tzinfo=UTC),
                value="99.0000",
            ),
            _fact(
                batch_id="batch:conflict",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000001",
                accepted=accepted,
                value="100.0000",
            ),
            _fact(
                batch_id="batch:conflict",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000002",
                accepted=accepted,
                value="101.0000",
            ),
        ],
        source_sha256=source_sha,
    )
    store = tmp_path / "store"

    result = PitStoreBuilder(output_dir=store).build([conflict])

    assert pq.read_table(result.table_path("facts_pit")).num_rows == 0
    assert pq.read_table(result.table_path("facts_latest")).num_rows == 0
    conflicts = pq.read_table(result.table_path("facts_conflicts"))
    assert conflicts.schema.equals(PIT_CONFLICT_SCHEMA, check_metadata=True)
    assert conflicts.to_pylist() == [
        {
            "cik": 1234567,
            "tag": "Revenue",
            "ddate": date(2026, 3, 31),
            "qtrs": 1,
            "uom": "USD",
            "accepted": accepted,
            "distinct_values": 2,
            "candidate_rows": 2,
            "adshs": ["0001234567-26-000001", "0001234567-26-000002"],
            "values": ["100.0000", "101.0000"],
        }
    ]


def test_identical_reprocessed_fact_is_coalesced_deterministically(tmp_path: Path) -> None:
    accepted = datetime(2026, 5, 1, 20, tzinfo=UTC)
    first_sha = "4" * 64
    second_sha = "5" * 64
    first = _batch(
        tmp_path,
        "first",
        [
            _fact(
                batch_id="batch:first",
                source_quarter="2026q2",
                source_sha256=first_sha,
                adsh="0001234567-26-000001",
                accepted=accepted,
                value="100.0000",
            )
        ],
        source_sha256=first_sha,
    )
    repeated_row = _fact(
        batch_id="batch:second",
        source_quarter="2026q2",
        source_sha256=second_sha,
        adsh="0001234567-26-000001",
        accepted=accepted,
        value="100.0000",
    )
    second = _batch(
        tmp_path,
        "second",
        [repeated_row],
        source_sha256=second_sha,
    )

    result = PitStoreBuilder(output_dir=tmp_path / "store").build([second, first])
    pit = pq.read_table(result.table_path("facts_pit")).to_pylist()
    latest = pq.read_table(result.table_path("facts_latest")).to_pylist()

    assert len(pit) == len(latest) == 1
    assert pit[0]["batch_id"] == latest[0]["batch_id"] == "batch:first"


def test_equal_time_equal_value_from_distinct_accessions_is_not_a_false_conflict(
    tmp_path: Path,
) -> None:
    source_sha = "9" * 64
    accepted = datetime(2026, 5, 1, 20, tzinfo=UTC)
    duplicate = _batch(
        tmp_path,
        "duplicate-accessions",
        [
            _fact(
                batch_id="batch:duplicate-accessions",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000002",
                accepted=accepted,
                value="100.0000",
            ),
            _fact(
                batch_id="batch:duplicate-accessions",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000001",
                accepted=accepted,
                value="100.0000",
            ),
        ],
        source_sha256=source_sha,
    )

    result = PitStoreBuilder(output_dir=tmp_path / "store").build([duplicate])

    pit = pq.read_table(result.table_path("facts_pit")).to_pylist()
    assert len(pit) == 1
    assert pit[0]["adsh"] == "0001234567-26-000001"
    assert pit[0]["value"] == Decimal("100.0000")


def test_input_and_cached_artifact_corruption_are_rejected(tmp_path: Path) -> None:
    base, _ = _base_and_amendment(tmp_path)
    builder = PitStoreBuilder(output_dir=tmp_path / "store")
    result = builder.build([base])

    manifest_path = result.output_dir / "pit_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["selection"]["facts_pit"] = "maximum accepted"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PitStoreError, match="contradicts"):
        builder.build([base])

    manifest["selection"]["facts_pit"] = "minimum accepted"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result.table_path("facts_pit").write_bytes(b"corrupt")
    with pytest.raises(PitStoreError, match="verification failed"):
        builder.build([base])

    bad_descriptor = PitInputBatch(
        batch_id=base.batch_id,
        source_quarter=base.source_quarter,
        source_sha256=base.source_sha256,
        facts_path=base.facts_path,
        facts_content_sha256="f" * 64,
        byte_count=base.byte_count,
        row_count=base.row_count,
        arrow_schema=base.arrow_schema,
    )
    with pytest.raises(PitStoreError, match="input hash"):
        PitStoreBuilder(output_dir=tmp_path / "other").build([bad_descriptor])


def test_zero_rows_are_valid_and_manifest_is_complete(tmp_path: Path) -> None:
    empty = _batch(tmp_path, "empty", [], source_sha256="6" * 64)
    result = PitStoreBuilder(output_dir=tmp_path / "store").build([empty])

    assert all(table.row_count == 0 for table in result.tables)
    manifest = json.loads((result.output_dir / "pit_manifest.json").read_text())
    assert manifest["pit_key"] == ["cik", "tag", "ddate", "qtrs", "uom"]
    assert manifest["selection"] == {
        "equal_time_conflicts": "quarantine_key_and_count",
        "facts_latest": "maximum accepted",
        "facts_pit": "minimum accepted",
    }


def test_inconsistent_consolidation_or_batch_provenance_fails(tmp_path: Path) -> None:
    source_sha = "7" * 64
    inconsistent = _batch(
        tmp_path,
        "inconsistent",
        [
            _fact(
                batch_id="wrong-batch",
                source_quarter="2026q2",
                source_sha256=source_sha,
                adsh="0001234567-26-000001",
                accepted=datetime(2026, 5, 1, 20, tzinfo=UTC),
                value="100.0000",
                is_consolidated=True,
                coreg="Subsidiary",
            )
        ],
        source_sha256=source_sha,
    )

    with pytest.raises(PitStoreError, match="invariants"):
        PitStoreBuilder(output_dir=tmp_path / "store").build([inconsistent])


def test_safe_reader_rejects_naive_time_unknown_columns_and_retroactive_field_usage(
    tmp_path: Path,
) -> None:
    empty = _batch(tmp_path, "empty", [], source_sha256="8" * 64)
    result = PitStoreBuilder(output_dir=tmp_path / "store").build([empty])
    path = result.table_path("facts_pit")

    with pytest.raises(EdgarConfigurationError, match="timezone-aware"):
        read_pit_facts_as_of(path, datetime(2026, 5, 1))
    with pytest.raises(PitStoreError, match="unknown column"):
        read_pit_facts_as_of(path, datetime(2026, 5, 1, tzinfo=UTC), columns=["unknown"])
    assert "prevrpt" not in Path(pit_store_module.__file__).read_text(encoding="utf-8")
