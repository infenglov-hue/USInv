from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverAcquisitionResult,
    CoverArchiveRecord,
    CoverFormHistoryProof,
    CoverFpiFormObservation,
    CoverShareObservation,
)
from usinv.data.edgar.cover_shards import (
    CoverEvidenceMerge,
    materialize_cover_evidence_merge,
    materialize_cover_evidence_shard,
    merge_cover_evidence_shards,
    read_cover_evidence_shard,
    read_cover_evidence_snapshot,
    reconcile_cover_evidence_merge,
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
        fpi_form_observations=(
            CoverFpiFormObservation(
                cik,
                f"{cik:010d}-20-000001",
                "20-F",
                datetime(2020, 3, 1, 20, tzinfo=UTC),
                f"sec://{cik}/history#20-f",
            ),
        ),
        form_history_proofs=(
            CoverFormHistoryProof(
                cik,
                CUTOFF,
                (f"https://data.sec.gov/submissions/{cik}#" + "d" * 64,),
                f"sec-submissions-complete://{cik}/" + "e" * 64,
            ),
        ),
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
    assert reopened.fpi_form_observations == created.fpi_form_observations
    assert reopened.form_history_proofs == created.form_history_proofs

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
    assert len(merged.fpi_form_observations) == 2
    assert len(merged.form_history_proofs) == 2
    assert merged.master.resolve("TWO", "NASDAQ", date(2026, 6, 1)).status == "mapped"

    created = materialize_cover_evidence_merge(merged, tmp_path / "final")
    cached = materialize_cover_evidence_merge(merged, tmp_path / "final")
    assert created.snapshot_id == cached.snapshot_id
    assert not created.from_cache and cached.from_cache
    reopened = read_cover_evidence_snapshot(created.output_dir)
    assert reopened.merge.share_observations == merged.share_observations
    assert reopened.merge.fpi_form_observations == merged.fpi_form_observations
    assert reopened.merge.form_history_proofs == merged.form_history_proofs
    assert reopened.merge.master == merged.master

    created.output_dir.joinpath("complete-evidence.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EdgarPayloadError, match="identity"):
        read_cover_evidence_snapshot(created.output_dir)

    with pytest.raises(EdgarPayloadError, match="exactly cover"):
        merge_cover_evidence_shards((first,), expected_ciks=(1, 2))
    with pytest.raises(EdgarPayloadError, match="overlap"):
        merge_cover_evidence_shards((first, first), expected_ciks=(1,))


def test_complete_cover_reader_accepts_verified_v2_input_for_reconciliation(
    tmp_path: Path,
) -> None:
    merged = merge_cover_evidence_shards(
        (materialize_cover_evidence_shard(*_inputs(1, "ONE"), tmp_path / "shard"),),
        expected_ciks=(1,),
    )
    created = materialize_cover_evidence_merge(merged, tmp_path / "current")
    payload = json.loads(created.output_dir.joinpath("complete-evidence.json").read_text())
    payload["version"] = "usinv-cover-evidence-merge-v2"
    snapshot_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    legacy = tmp_path / "legacy" / snapshot_id
    shutil.copytree(created.output_dir, legacy)
    legacy.joinpath("complete-evidence.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    reopened = read_cover_evidence_snapshot(legacy)

    assert reopened.snapshot_id == snapshot_id
    assert reopened.merge.master == merged.master


def test_cover_reconciliation_collapses_equivalent_equity_wording_and_rekeys_shares() -> None:
    cik = 42
    first_anchor = 'sec-cover-class:"common stock"'
    second_anchor = 'sec-cover-class:"common stock, $0.01 par value per share"'
    first_id = mint_security_id(cik, first_anchor)
    second_id = mint_security_id(cik, second_anchor)
    securities = (
        Security(
            first_id,
            cik,
            "Common Stock",
            "common_stock",
            True,
            first_anchor,
            "sec_xbrl_cover",
            "sec://42/first",
        ),
        Security(
            second_id,
            cik,
            "Common Stock, $0.01 par value per share",
            "common_stock",
            True,
            second_anchor,
            "sec_xbrl_cover",
            "sec://42/second",
        ),
    )
    symbols = (
        SymbolInterval(
            first_id,
            "SAME",
            "NASDAQ",
            date(2026, 4, 1),
            None,
            "sec_xbrl_cover",
            "high",
            "sec://42/first",
            datetime(2026, 4, 1, 20, tzinfo=UTC),
            "historical_interval",
        ),
        SymbolInterval(
            second_id,
            "SAME",
            "NASDAQ",
            date(2026, 5, 1),
            None,
            "sec_xbrl_cover",
            "high",
            "sec://42/second",
            datetime(2026, 5, 1, 20, tzinfo=UTC),
            "historical_interval",
        ),
    )
    shares = (
        CoverShareObservation(
            first_id,
            cik,
            datetime(2026, 4, 1, 20, tzinfo=UTC),
            Decimal("100"),
            "sec://42/first#shares",
        ),
        CoverShareObservation(
            second_id,
            cik,
            datetime(2026, 5, 1, 20, tzinfo=UTC),
            Decimal("110"),
            "sec://42/second#shares",
        ),
    )
    merged = CoverEvidenceMerge(
        PLAN,
        CUTOFF,
        (cik,),
        ("b" * 64,),
        (),
        shares,
        (),
        (),
        (),
        (),
        build_security_master(securities, symbols),
    )

    result = reconcile_cover_evidence_merge(merged)

    assert result.collapsed_groups == 1
    assert result.rewritten_security_ids == 2
    assert result.ambiguous_groups == 0
    assert len(result.merge.master.securities) == 1
    assert not result.merge.master.issues
    mapped = result.merge.master.resolve("SAME", "NASDAQ", date(2026, 6, 1))
    assert mapped.status == "mapped"
    assert {row.security_id for row in result.merge.share_observations} == {mapped.security_id}


def test_cover_reconciliation_keeps_concurrent_generic_classes_ambiguous() -> None:
    cik = 43
    anchors = (
        'sec-cover-class:[["us-gaap:StatementClassOfStockAxis","one:CommonStockMember"]]',
        'sec-cover-class:[["us-gaap:StatementClassOfStockAxis","two:CommonStockMember"]]',
    )
    security_ids = tuple(mint_security_id(cik, anchor) for anchor in anchors)
    securities = tuple(
        Security(
            security_id,
            cik,
            "Common Stock",
            "common_stock",
            True,
            anchor,
            "sec_xbrl_cover",
            f"sec://43/{ticker}",
        )
        for security_id, anchor, ticker in zip(
            security_ids,
            anchors,
            ("CLASSA", "CLASSB"),
            strict=True,
        )
    )
    symbols = tuple(
        SymbolInterval(
            security_id,
            ticker,
            "NASDAQ",
            date(2026, 5, 1),
            None,
            "sec_xbrl_cover",
            "high",
            f"sec://43/{ticker}",
            datetime(2026, 5, 1, 20, tzinfo=UTC),
            "historical_interval",
        )
        for security_id, ticker in zip(security_ids, ("CLASSA", "CLASSB"), strict=True)
    )
    merged = CoverEvidenceMerge(
        PLAN,
        CUTOFF,
        (cik,),
        ("b" * 64,),
        (),
        (),
        (),
        (),
        (),
        (),
        build_security_master(securities, symbols),
    )

    result = reconcile_cover_evidence_merge(merged)

    assert result.ambiguous_groups == 1
    assert result.rewritten_security_ids == 0
    assert result.merge.master.securities == merged.master.securities


def test_cover_reconciliation_preserves_ticker_change_pit_boundaries() -> None:
    cik = 44
    anchors = (
        'sec-cover-class:"class a common stock"',
        'sec-cover-class:"class a common stock, $0.01 par value per share"',
    )
    security_ids = tuple(mint_security_id(cik, anchor) for anchor in anchors)
    securities = tuple(
        Security(
            security_id,
            cik,
            title,
            "common_stock",
            True,
            anchor,
            "sec_xbrl_cover",
            f"sec://44/{ticker}",
        )
        for security_id, anchor, title, ticker in zip(
            security_ids,
            anchors,
            ("Class A Common Stock", "Class A Common Stock, $0.01 par value per share"),
            ("OLD", "NEW"),
            strict=True,
        )
    )
    symbols = tuple(
        SymbolInterval(
            security_id,
            ticker,
            "NASDAQ",
            valid_from,
            None,
            "sec_xbrl_cover",
            "high",
            f"sec://44/{ticker}",
            datetime.combine(valid_from, datetime.min.time(), tzinfo=UTC),
            "historical_interval",
        )
        for security_id, ticker, valid_from in zip(
            security_ids,
            ("OLD", "NEW"),
            (date(2026, 4, 1), date(2026, 6, 1)),
            strict=True,
        )
    )
    merged = CoverEvidenceMerge(
        PLAN,
        CUTOFF,
        (cik,),
        ("b" * 64,),
        (),
        (),
        (),
        (),
        (),
        (),
        build_security_master(securities, symbols),
    )

    master = reconcile_cover_evidence_merge(merged).merge.master

    assert master.resolve("OLD", "NASDAQ", date(2026, 5, 31)).status == "mapped"
    assert master.resolve("OLD", "NASDAQ", date(2026, 6, 1)).status == "unmapped"
    assert master.resolve("NEW", "NASDAQ", date(2026, 5, 31)).status == "unmapped"
    assert master.resolve("NEW", "NASDAQ", date(2026, 6, 1)).status == "mapped"
