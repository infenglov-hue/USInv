from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from secfsdstools.a_utils.constants import PA_SCHEMA_MAP

from usinv.data.edgar.bulk import (
    BulkDownloadResponse,
    FsdsArchiveClient,
    FsdsArchiveError,
)
from usinv.data.edgar.client import EdgarConfigurationError
from usinv.data.edgar.fsds import (
    EXPECTED_RAW_COLUMNS,
    FsdsIngestError,
    FsdsIngestor,
    FsdsSchemaError,
    read_consolidated_facts_as_of,
)

SUB_COLUMNS = tuple(PA_SCHEMA_MAP["sub.txt"].names)
NUM_COLUMNS = tuple(PA_SCHEMA_MAP["num.txt"].names)
PRE_COLUMNS = tuple(PA_SCHEMA_MAP["pre.txt"].names)
TAG_COLUMNS = (
    "tag",
    "version",
    "custom",
    "abstract",
    "datatype",
    "iord",
    "crdr",
    "tlabel",
    "doc",
)


def _sub_row(
    adsh: str,
    *,
    form: str,
    accepted: str,
    prevrpt: str,
    name: str = "Fixture Corp",
) -> dict[str, str]:
    values = dict.fromkeys(SUB_COLUMNS, "")
    values.update(
        {
            "adsh": adsh,
            "cik": "1234567",
            "name": name,
            "sic": "3571.0",
            "countryinc": "US",
            "former": "Old Fixture Corp" if prevrpt == "1" else "",
            "changed": "20200101" if prevrpt == "1" else "",
            "afs": "SML",
            "wksi": "0",
            "fye": "1231",
            "form": form,
            "period": "20260331",
            "fy": "2026.0",
            "fp": "Q1",
            "filed": accepted[:10].replace("-", ""),
            "accepted": accepted,
            "prevrpt": prevrpt,
            "detail": "1",
            "instance": "fix-20260331.xml",
            "nciks": "1",
        }
    )
    return values


def _num_row(
    adsh: str,
    value: str,
    *,
    segments: str = "",
    coreg: str = "",
    tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
) -> dict[str, str]:
    return {
        "adsh": adsh,
        "tag": tag,
        "version": "us-gaap/2026",
        "ddate": "20260331",
        "qtrs": "1",
        "uom": "USD",
        "segments": segments,
        "coreg": coreg,
        "value": value,
        "footnote": "",
    }


def _pre_row(adsh: str) -> dict[str, str]:
    return {
        "adsh": adsh,
        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "version": "us-gaap/2026",
        "report": "1",
        "line": "1",
        "stmt": "IS",
        "inpth": "0",
        "rfile": "H",
        "plabel": "Revenue",
        "negating": "0",
    }


def _tag_row() -> dict[str, str]:
    return {
        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "version": "us-gaap/2026",
        "custom": "0",
        "abstract": "0",
        "datatype": "monetary",
        "iord": "D",
        "crdr": "C",
        "tlabel": "Revenue",
        "doc": "Revenue from contracts with customers.",
    }


def _tsv(columns: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(columns)]
    lines.extend("\t".join(row[column] for column in columns) for row in rows)
    return "\n".join(lines) + "\n"


def _fsds_zip(
    *,
    original_value: str = "123456789.1234",
    amendment_value: str = "130000000.0000",
    tag_columns: tuple[str, ...] = TAG_COLUMNS,
    unknown_adsh: bool = False,
) -> bytes:
    original = "0001234567-26-000001"
    amendment = "0001234567-26-000002"
    submissions = [
        _sub_row(
            original,
            form="10-Q",
            accepted="2026-05-01 16:00:00",
            prevrpt="1",
        ),
        _sub_row(
            amendment,
            form="10-Q/A",
            accepted="2026-05-08 17:31:00",
            prevrpt="0",
        ),
    ]
    facts = [
        _num_row(original, original_value),
        _num_row(original, "100.0000", segments='{"LegalEntityAxis":"RetailMember"}'),
        _num_row(original, "50.0000", coreg="Fixture Guarantor"),
        _num_row("0001234567-26-999999" if unknown_adsh else amendment, amendment_value),
    ]
    presentations = [_pre_row(original), _pre_row(amendment)]
    tag = _tag_row()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sub.txt", _tsv(SUB_COLUMNS, submissions))
        archive.writestr("num.txt", _tsv(NUM_COLUMNS, facts))
        archive.writestr("pre.txt", _tsv(PRE_COLUMNS, presentations))
        archive.writestr("tag.txt", _tsv(tag_columns, [tag]))
    return output.getvalue()


class FakeTransport:
    def __init__(self, *bodies: bytes) -> None:
        self.bodies = list(bodies)

    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        destination: Path,
    ) -> BulkDownloadResponse:
        del url, headers, timeout_seconds
        body = self.bodies.pop(0)
        destination.write_bytes(body)
        return BulkDownloadResponse(
            status=200,
            headers={},
            byte_count=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
        )


def _archive(
    tmp_path: Path,
    *bodies: bytes,
    now: Callable[[], datetime] | None = None,
) -> FsdsArchiveClient:
    kwargs = {"now": now} if now is not None else {}
    return FsdsArchiveClient(
        contact_email="ops@usinv.dev",
        archive_dir=tmp_path / "archive",
        transport=FakeTransport(*bodies),
        max_requests_per_second=8,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
        **kwargs,
    )


def test_ingest_preserves_four_raw_tables_and_decimal_fact_provenance(tmp_path: Path) -> None:
    archive = _archive(tmp_path, _fsds_zip())
    archived = archive.sync_quarter("2026q2").record
    ingestor = FsdsIngestor(
        archive=archive,
        output_dir=tmp_path / "parquet",
        now=lambda: datetime(2026, 7, 18, tzinfo=UTC),
    )

    result = ingestor.ingest_record(archived)

    assert not result.from_cache
    assert {table.name for table in result.tables} == {
        "raw_sub",
        "raw_num",
        "raw_pre",
        "raw_tag",
        "filings",
        "facts_raw",
    }
    raw_num = pq.read_table(result.table_path("raw_num"))
    assert all(pa.types.is_string(field.type) for field in raw_num.schema)
    assert raw_num.column("value")[0].as_py() == "123456789.1234"
    assert pq.read_table(result.table_path("raw_tag")).num_rows == 1

    filings = pq.read_table(result.table_path("filings")).to_pylist()
    assert filings[0]["accepted"] == datetime(2026, 5, 1, 20, 0, tzinfo=UTC)
    assert filings[0]["prevrpt"] is True
    facts = pq.read_table(result.table_path("facts_raw"))
    assert facts.num_rows == 4
    assert facts.schema.field("value").type == pa.decimal128(28, 4)
    assert facts.column("value")[0].as_py() == Decimal("123456789.1234")
    manifest = json.loads((result.output_dir / "ingest_manifest.json").read_text())
    assert manifest["secfsdstools_version"] == "2.4.3"
    assert manifest["source"]["content_sha256"] == archived.content_sha256


def test_as_of_reader_blocks_future_amendment_segments_and_coreg_without_prevrpt_filter(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path, _fsds_zip())
    archive.sync_quarter("2026q2")
    result = FsdsIngestor(archive=archive, output_dir=tmp_path / "parquet").ingest_quarter("2026q2")

    early = read_consolidated_facts_as_of(
        result.table_path("facts_raw"),
        datetime(2026, 5, 2, tzinfo=UTC),
        columns=["adsh", "value", "filing_prevrpt", "is_consolidated"],
    ).to_pylist()
    later = read_consolidated_facts_as_of(
        result.table_path("facts_raw"), datetime(2026, 5, 9, tzinfo=UTC)
    )

    assert len(early) == 1
    assert early[0]["value"] == Decimal("123456789.1234")
    assert early[0]["filing_prevrpt"] is True
    assert later.num_rows == 2
    with pytest.raises(EdgarConfigurationError, match="timezone-aware"):
        read_consolidated_facts_as_of(result.table_path("facts_raw"), datetime(2026, 5, 9))


def test_same_source_is_verified_cache_hit_and_corruption_fails(tmp_path: Path) -> None:
    archive = _archive(tmp_path, _fsds_zip())
    record = archive.sync_quarter("2026q2").record
    ingestor = FsdsIngestor(archive=archive, output_dir=tmp_path / "parquet")
    first = ingestor.ingest_record(record)

    cached = ingestor.ingest_record(record)
    assert cached.from_cache and cached.output_dir == first.output_dir

    manifest_path = first.output_dir / "ingest_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"].append(manifest["tables"][0])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FsdsIngestError, match="contradicts"):
        ingestor.ingest_record(record)

    manifest["tables"].pop()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    first.table_path("facts_raw").write_bytes(b"corrupt")
    with pytest.raises(FsdsIngestError, match="verification failed"):
        ingestor.ingest_record(record)


def test_reprocessed_zip_gets_separate_parquet_version_and_archive_as_of(tmp_path: Path) -> None:
    first_seen = datetime(2026, 7, 1, tzinfo=UTC)
    second_seen = datetime(2026, 7, 18, tzinfo=UTC)
    moments = iter((first_seen, second_seen))
    archive = _archive(
        tmp_path,
        _fsds_zip(original_value="100.0000"),
        _fsds_zip(original_value="200.0000"),
        now=lambda: next(moments),
    )
    first_record = archive.sync_quarter("2026q2").record
    second_record = archive.sync_quarter("2026q2", refresh=True).record
    ingestor = FsdsIngestor(archive=archive, output_dir=tmp_path / "parquet")

    first = ingestor.ingest_quarter("2026q2", archive_as_of=first_seen)
    second = ingestor.ingest_quarter("2026q2")

    assert first.source_sha256 == first_record.content_sha256
    assert second.source_sha256 == second_record.content_sha256
    assert first.output_dir != second.output_dir
    assert first.output_dir.is_dir() and second.output_dir.is_dir()


@pytest.mark.parametrize(
    "body, message",
    [
        (_fsds_zip(tag_columns=TAG_COLUMNS[:-1]), "schema drift"),
        (_fsds_zip(unknown_adsh=True), "unknown submission"),
        (_fsds_zip(original_value="1.00001"), "NUMERIC\\(28,4\\)"),
    ],
)
def test_schema_join_and_decimal_failures_leave_no_published_batch(
    tmp_path: Path, body: bytes, message: str
) -> None:
    archive = _archive(tmp_path, body)
    archive.sync_quarter("2026q2")
    output = tmp_path / "parquet"
    ingestor = FsdsIngestor(archive=archive, output_dir=output)

    with pytest.raises(FsdsSchemaError, match=message):
        ingestor.ingest_quarter("2026q2")

    assert not tuple(output.glob("2026q2/*"))


def test_empty_headers_only_archive_produces_six_valid_zero_row_tables(tmp_path: Path) -> None:
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", compression=zipfile.ZIP_DEFLATED) as archive_zip:
        archive_zip.writestr("sub.txt", _tsv(SUB_COLUMNS, []))
        archive_zip.writestr("num.txt", _tsv(NUM_COLUMNS, []))
        archive_zip.writestr("pre.txt", _tsv(PRE_COLUMNS, []))
        archive_zip.writestr("tag.txt", _tsv(TAG_COLUMNS, []))
    archive = _archive(tmp_path, body.getvalue())
    archive.sync_quarter("2009q1")

    result = FsdsIngestor(archive=archive, output_dir=tmp_path / "parquet").ingest_quarter("2009q1")

    assert len(result.tables) == 6
    assert all(table.row_count == 0 for table in result.tables)


def test_vendored_standardizer_snapshot_and_q4_contract_are_hash_locked() -> None:
    vendor_root = (
        Path(__file__).parents[1] / "usinv" / "data" / "edgar" / "vendor" / "secfsdstools_2_4_3"
    )
    source = json.loads((vendor_root / "SOURCE.json").read_text(encoding="utf-8"))
    assert source["commit"] == "af83c24f999109322d01b4980d207eec67bc749e"
    for relative_path, expected_hash in source["files"].items():
        actual = hashlib.sha256((vendor_root / relative_path).read_bytes()).hexdigest()
        assert actual == expected_hash

    rules_path = Path(__file__).parents[1] / "usinv" / "data" / "edgar" / "rules"
    rules = yaml.safe_load((rules_path / "quarterly_v1.yaml").read_text(encoding="utf-8"))
    assert rules["q4"]["formula"] == "fiscal_year_value - sum(q1, q2, q3)"
    assert rules["q4"]["required_direct_quarter_count"] == 3
    assert rules["availability"] == {
        "field": "accepted",
        "aggregation": "max",
        "timezone": "UTC",
    }


def test_secfsdstools_contract_is_pinned_but_usinv_covers_missing_tag_and_decimal() -> None:
    assert "tag.txt" not in PA_SCHEMA_MAP
    assert PA_SCHEMA_MAP["num.txt"].field("value").type == pa.float64()
    assert EXPECTED_RAW_COLUMNS["tag.txt"] == frozenset(TAG_COLUMNS)
    assert "segments" in EXPECTED_RAW_COLUMNS["num.txt"]


def test_archive_rejects_a_record_from_another_manifest(tmp_path: Path) -> None:
    first = _archive(tmp_path / "one", _fsds_zip())
    foreign_record = first.sync_quarter("2026q2").record
    second = _archive(tmp_path / "two", _fsds_zip(original_value="999.0000"))
    second.sync_quarter("2026q2")

    with pytest.raises(FsdsArchiveError, match="not present"):
        FsdsIngestor(archive=second, output_dir=tmp_path / "parquet").ingest_record(foreign_record)
