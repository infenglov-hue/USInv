from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverAcquisitionResult,
    CoverArchiveRecord,
)
from usinv.data.edgar.cover_shards import (
    materialize_cover_evidence_shard,
    merge_cover_evidence_shards,
    read_cover_evidence_shard,
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
    acquisition = CoverAcquisitionResult(
        plan_snapshot_id=PLAN,
        as_of=CUTOFF,
        requested_ciks=(cik,),
        deferred_ciks=(),
        start_after_cik=None,
        selected_filings=1,
        archived_filings=1,
        archives=(CoverArchiveRecord(cik, accession, "b" * 64, "c" * 64),),
        evidence=(object(),),  # type: ignore[arg-type]
        gaps=(CoverAcquisitionGap(cik, None, "fixture_gap", "visible fixture gap"),),
    )
    bootstrap = CoverSecurityBootstrap(_master(cik, ticker), 1, 1, ())
    return acquisition, bootstrap


def test_cover_evidence_shard_is_immutable_compact_and_verified(tmp_path: Path) -> None:
    acquisition, bootstrap = _inputs(1, "ONE")

    created = materialize_cover_evidence_shard(acquisition, bootstrap, tmp_path)
    cached = materialize_cover_evidence_shard(acquisition, bootstrap, tmp_path)

    assert created.snapshot_id == cached.snapshot_id
    assert created.requested_ciks == (1,)
    assert created.master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"
    assert created.acquisition_gaps[0].kind == "fixture_gap"
    assert read_cover_evidence_shard(created.output_dir).snapshot_id == created.snapshot_id

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
    assert merged.master.resolve("TWO", "NASDAQ", date(2026, 6, 1)).status == "mapped"

    with pytest.raises(EdgarPayloadError, match="exactly cover"):
        merge_cover_evidence_shards((first,), expected_ciks=(1, 2))
    with pytest.raises(EdgarPayloadError, match="overlap"):
        merge_cover_evidence_shards((first, first), expected_ciks=(1,))
