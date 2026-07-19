from datetime import UTC, datetime
from pathlib import Path

import pytest

from usinv.cli import main
from usinv.data.edgar.bulk import FsdsArchiveRecord, FsdsQuarter, FsdsSyncResult
from usinv.data.edgar.client import EdgarClient, EdgarDocument
from usinv.data.edgar.fsds import FsdsIngestor, FsdsIngestResult, FsdsTableArtifact
from usinv.data.edgar.pit_store import (
    PitInputBatch,
    PitStoreBuilder,
    PitStoreResult,
    PitTableArtifact,
)
from usinv.data.edgar.tag_chains import CHAIN_VERSION, CoverageReport


def _document(payload: dict[str, object]) -> EdgarDocument:
    return EdgarDocument(
        url="https://data.sec.gov/fixture",
        retrieved_at=datetime(2026, 7, 18, tzinfo=UTC),
        validated_at=datetime(2026, 7, 18, tzinfo=UTC),
        content_sha256="a" * 64,
        payload=payload,
        from_cache=False,
        revalidated=False,
    )


def test_config_check_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config-check"]) == 0
    output = capsys.readouterr().out
    assert "config_ok schema=1" in output
    assert "evidence=research execution=paper holdings=15 overlay=O0" in output


def test_edgar_smoke_fails_closed_without_contact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("USINV_EDGAR_EMAIL", raising=False)

    assert main(["edgar-smoke"]) == 2
    assert "set USINV_EDGAR_EMAIL" in capsys.readouterr().err


def test_edgar_smoke_reports_both_documents_without_dumping_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def submissions(self, cik: str, *, refresh: bool) -> EdgarDocument:
            assert cik == "0000320193" and refresh
            return _document({"name": "Apple Inc.", "filings": {}, "cik": "320193"})

        def companyfacts(self, cik: str, *, refresh: bool) -> EdgarDocument:
            assert cik == "0000320193" and refresh
            return _document({"entityName": "Apple Inc.", "facts": {}, "cik": 320193})

    monkeypatch.setattr(
        EdgarClient,
        "from_config",
        staticmethod(lambda config, *, cache_dir=None: FakeClient()),
    )

    assert main(["edgar-smoke", "--refresh"]) == 0
    output = capsys.readouterr().out
    assert "edgar_smoke_ok cik=0000320193 name='Apple Inc.'" in output
    assert "submissions_sha256=" in output and "companyfacts_sha256=" in output
    assert "accessionNumber" not in output


def test_fsds_sync_reports_archive_results_without_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from usinv.data.edgar.bulk import FsdsArchiveClient

    class FakeArchive:
        def sync_quarter(self, quarter: FsdsQuarter, *, refresh: bool) -> FsdsSyncResult:
            assert refresh
            return FsdsSyncResult(
                FsdsArchiveRecord(
                    quarter=quarter,
                    source_url=quarter.url,
                    checked_at=datetime(2026, 7, 18, tzinfo=UTC),
                    content_sha256="a" * 64,
                    byte_count=123,
                    object_path=f"objects/{quarter}/{'a' * 64}.zip",
                    members=("num.txt", "pre.txt", "sub.txt", "tag.txt"),
                    outcome="initial",
                    previous_sha256=None,
                    http_status=200,
                    etag=None,
                    last_modified=None,
                ),
                network_accessed=True,
                new_version=True,
            )

    monkeypatch.setattr(
        FsdsArchiveClient,
        "from_config",
        staticmethod(lambda config, *, archive_dir=None: FakeArchive()),
    )

    assert main(["fsds-sync", "--start", "2025q4", "--end", "2026q1", "--refresh"]) == 0
    output = capsys.readouterr().out
    assert "fsds_quarter_ok quarter=2025q4 state=initial sha256=" in output
    assert "fsds_sync_ok quarters=2 new_versions=2 reprocessed=0" in output
    assert "header" not in output


def test_fsds_ingest_reports_only_counts_and_source_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeIngestor:
        def ingest_quarter(
            self, quarter: FsdsQuarter, *, archive_as_of: datetime | None
        ) -> FsdsIngestResult:
            assert archive_as_of == datetime(2026, 7, 1, tzinfo=UTC)
            tables = (
                FsdsTableArtifact("filings", "filings.parquet", 2, 100, "b" * 64, "schema"),
                FsdsTableArtifact("facts_raw", "facts_raw.parquet", 4, 200, "c" * 64, "schema"),
            )
            return FsdsIngestResult(
                quarter=quarter,
                source_sha256="a" * 64,
                batch_id=f"fixture:{quarter}",
                output_dir=Path("unused"),
                created_at=datetime(2026, 7, 18, tzinfo=UTC),
                tables=tables,
                from_cache=False,
            )

    monkeypatch.setattr(
        FsdsIngestor,
        "from_config",
        staticmethod(lambda config, *, archive_dir=None, output_dir=None: FakeIngestor()),
    )

    assert (
        main(
            [
                "fsds-ingest",
                "--start",
                "2026q1",
                "--end",
                "2026q2",
                "--archive-as-of",
                "2026-07-01T00:00:00+00:00",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "fsds_ingest_quarter_ok quarter=2026q1 state=created source_sha256=" in output
    assert "filings=2 facts_raw=4" in output
    assert "fsds_ingest_ok quarters=2" in output
    assert "Fixture Corp" not in output


def test_fsds_ingest_rejects_naive_archive_cutoff() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "fsds-ingest",
                "--end",
                "2026q1",
                "--archive-as-of",
                "2026-07-01T00:00:00",
            ]
        )


def test_pit_build_reports_only_snapshot_hash_and_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive_as_of = datetime(2026, 7, 1, tzinfo=UTC)
    ingest_result = FsdsIngestResult(
        quarter=FsdsQuarter(2026, 1),
        source_sha256="a" * 64,
        batch_id="fixture:2026q1",
        output_dir=Path("unused"),
        created_at=datetime(2026, 7, 18, tzinfo=UTC),
        tables=(FsdsTableArtifact("facts_raw", "facts_raw.parquet", 3, 200, "b" * 64, "schema"),),
        from_cache=True,
    )

    class FakeIngestor:
        def ingest_range(
            self,
            start: FsdsQuarter,
            end: FsdsQuarter,
            *,
            archive_as_of: datetime | None,
        ) -> tuple[FsdsIngestResult, ...]:
            assert start == end == FsdsQuarter(2026, 1)
            assert archive_as_of == datetime(2026, 7, 1, tzinfo=UTC)
            return (ingest_result,)

    class FakeBuilder:
        def build(self, inputs: list[PitInputBatch]) -> PitStoreResult:
            assert len(inputs) == 1 and inputs[0].batch_id == "fixture:2026q1"
            return PitStoreResult(
                snapshot_id="c" * 64,
                output_dir=Path("unused"),
                created_at=datetime(2026, 7, 18, tzinfo=UTC),
                inputs=tuple(inputs),
                tables=(
                    PitTableArtifact("facts_pit", "facts_pit.parquet", 2, 100, "d" * 64, "s"),
                    PitTableArtifact("facts_latest", "facts_latest.parquet", 2, 100, "e" * 64, "s"),
                ),
                from_cache=False,
            )

    monkeypatch.setattr(
        FsdsIngestor,
        "from_config",
        staticmethod(lambda config, *, archive_dir=None, output_dir=None: FakeIngestor()),
    )
    monkeypatch.setattr(
        PitStoreBuilder,
        "from_config",
        staticmethod(lambda config, *, output_dir=None: FakeBuilder()),
    )

    assert (
        main(
            [
                "pit-build",
                "--start",
                "2026q1",
                "--end",
                "2026q1",
                "--archive-as-of",
                archive_as_of.isoformat(),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "pit_build_ok state=created snapshot_id=" in output
    assert "inputs=1 facts_pit=2 facts_latest=2" in output
    assert "Fixture Corp" not in output


def test_pit_build_rejects_naive_archive_cutoff() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "pit-build",
                "--end",
                "2026q1",
                "--archive-as-of",
                "2026-07-01T00:00:00",
            ]
        )


def test_fundamentals_coverage_reports_and_enforces_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import usinv.cli as cli_module

    report = CoverageReport(
        sample_quarter="2025q4",
        chain_version=CHAIN_VERSION,
        eligible_issuers=10,
        concepts=(),
        core_rate=0.80,
        secondary_rate=0.70,
    )
    monkeypatch.setattr(cli_module, "standardize_pit_snapshot", lambda *args, **kwargs: (1, 2))
    monkeypatch.setattr(cli_module, "eligible_ciks_from_filings", lambda paths: {1, 2})
    monkeypatch.setattr(cli_module, "build_coverage_report", lambda *args, **kwargs: report)
    monkeypatch.setattr(cli_module, "coverage_input", lambda *args, **kwargs: None)
    output = tmp_path / "coverage.json"

    result = main(
        [
            "fundamentals-coverage",
            "--facts-pit",
            "facts_pit.parquet",
            "--pre",
            "pre.parquet",
            "--filings",
            "filings.parquet",
            "--sample-quarter",
            "2025q4",
            "--output",
            str(output),
            "--enforce",
        ]
    )

    assert result == 3
    assert (
        "core_rate=0.800000 secondary_rate=0.700000 strict_gate=fail "
        "enforcement=deferred_phase_1_5" in capsys.readouterr().out
    )
    assert output.read_text(encoding="utf-8") == report.to_json()
