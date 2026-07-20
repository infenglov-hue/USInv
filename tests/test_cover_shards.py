from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverAcquisitionResult,
    CoverArchiveRecord,
    CoverShareObservation,
)
from usinv.data.edgar.cover_shards import (
    materialize_cover_evidence_merge,
    materialize_cover_evidence_shard,
    merge_cover_evidence_shards,
    read_cover_evidence_shard,
    read_cover_evidence_snapshot,
)
from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.edgar.security_bootstrap import CoverSecurityBootstrap

CUTOFF = datetime(2026, 7, 17, 20, tzinfo=UTC)
PLAN = "a" * 64


def _master(cik: int, ticker: str):
    anchor = "sec-cover-class:common-stock"
    security_id = mint_security_id(cik, anchor)
    pointer = f"sec://{cik}/{ticker}/cover"
    security = Security(
        security_id,
        cik,
        "Common Stock",
        "common_stock",
        True,
        anchor,
        "sec_xbrl_cover",
        pointer,
    )
    symbol = SymbolInterval(
        security_id,
        ticker,
        "NASDAQ",
        date(2026, 5, 1),
        None,
        "sec_xbrl_cover",
        "high",
        pointer,
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        "historical_interval",
    )
    return build_security_master((security,), (symbol,))


def _inputs(cik: int, ticker: str):
    accession = f"{cik:010d}-26-000001"
    master = _master(cik, ticker)
    security_id = master.securities[0].security_id
    acquisition = CoverAcquisitionResult(
        plan_snapshot_id=PLAN,
        as_of=CUTOFF,
        requested_ciks=(cik,),
        deferred_ciks=(),
        start_after_cik=None,
        selected_filings=1,
        archived_filings=1,
        archives=(CoverArchiveRecord(cik, accession, "b" * 64, "c" * 64),),
        share_observations=(
            CoverShareObservation(
                security_id,
                cik,
                datetime(2026, 5, 1, 20, tzinfo=UTC),
                Decimal("125000000"),
                f"sec://{cik}/{ticker}/cover#shares",
            ),
        ),
        evidence=(object(),),  # type: ignore[arg-type]
        gaps=(CoverAcquisitionGap(cik, None, "fixture_gap", "visible fixture gap"),),
    )
    bootstrap = CoverSecurityBootstrap(master, 1, 1, ())
    return acquisition, bootstrap


def test_cover_evidence_shard_is_immutable_compact_and_verified(tmp_path: Path) -> None:
    acquisition, bootstrap = _inputs(1, "ONE")

    created = materialize_cover_evidence_shard(acquisition, bootstrap, tmp_path)
    cached = materialize_cover_evidence_shard(acquisition, bootstrap, tmp_path)

    assert created.snapshot_id == cached.snapshot_id
    assert created.requested_ciks == (1,)
    assert created.master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"
    assert created.share_observations[0].shares_outstanding == Decimal("125000000")
    assert created.acquisition_gaps[0].kind == "fixture_gap"
    reopened = read_cover_evidence_shard(created.output_dir)
    assert reopened.snapshot_id == created.snapshot_id
    assert reopened.share_observations == created.share_observations

    created.output_dir.joinpath("shard.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EdgarPayloadError, match="identity"):
        read_cover_evidence_shard(created.output_dir)


def test_cover_evidence_merge_requires_an_exact_non_overlapping_cik_partition(
    tmp_path: Path,
) -> None:
    first = materialize_cover_evidence_shard(*_inputs(1, "ONE"), tmp_path / "one")
    second = materialize_cover_evidence_shard(*_inputs(2, "TWO"), tmp_path / "two")

    merged = merge_cover_evidence_shards((first, second), expected_ciks=(1, 2))

    assert merged.requested_ciks == (1, 2)
    assert len(merged.master.securities) == 2
    assert len(merged.share_observations) == 2
    assert merged.master.resolve("TWO", "NASDAQ", date(2026, 6, 1)).status == "mapped"

    created = materialize_cover_evidence_merge(merged, tmp_path / "final")
    cached = materialize_cover_evidence_merge(merged, tmp_path / "final")
    assert created.snapshot_id == cached.snapshot_id
    assert not created.from_cache and cached.from_cache
    reopened = read_cover_evidence_snapshot(created.output_dir)
    assert reopened.merge.share_observations == merged.share_observations
    assert reopened.merge.master == merged.master

    created.output_dir.joinpath("complete-evidence.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EdgarPayloadError, match="identity"):
        read_cover_evidence_snapshot(created.output_dir)

    with pytest.raises(EdgarPayloadError, match="exactly cover"):
        merge_cover_evidence_shards((first,), expected_ciks=(1, 2))
    with pytest.raises(EdgarPayloadError, match="overlap"):
        merge_cover_evidence_shards((first, first), expected_ciks=(1,))
