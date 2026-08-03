from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from usinv.calendar import default_calendar
from usinv.data.edgar.bulk import FsdsQuarter
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverArchiveRecord,
    CoverFormHistoryProof,
    CoverFpiFormObservation,
    CoverShareObservation,
    CoverTerminalFormObservation,
)
from usinv.data.edgar.cover_shards import (
    CoverEvidenceMerge,
    materialize_cover_evidence_merge,
)
from usinv.data.edgar.fsds import FILINGS_SCHEMA, FsdsIngestResult, FsdsTableArtifact
from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan, FilingDiscoveryRow
from usinv.data.edgar.ttm import TtmFact
from usinv.data.prices.base import (
    PriceFetchResult,
    PriceProvider,
    PriceQuery,
    PriceSecurityBinding,
    PriceSourcePage,
    VendorBarIssue,
    VendorDailyBar,
    materialize_price_snapshot,
)
from usinv.data.prices.universe import (
    acquire_price_universe,
    build_price_universe_plan,
    read_price_universe_snapshot,
    rebase_price_universe_snapshot,
)
from usinv.data.universe_evidence import (
    FilingSicObservation,
    build_security_universe_evidence,
    filing_sic_observations,
)
from usinv.phase_2_3 import (
    Phase23BuildError,
    _active_listing_matches_discovery,
    _filing_sic_snapshot_observations,
    _identity_regime_evidence,
    _supplement_sic_observations,
    _validate_supplements_as_of,
    build_phase_2_3,
)

SIGNAL = datetime(2026, 7, 17, 20, tzinfo=UTC)
CIK = 1


def _master():
    security_id = mint_security_id(CIK, "sec-cover-class:common-stock")
    security = Security(
        security_id,
        CIK,
        "Common Stock",
        "common_stock",
        True,
        "sec-cover-class:common-stock",
        "sec_xbrl_cover",
        "sec://cover/identity",
    )
    symbol = SymbolInterval(
        security_id,
        "ONE",
        "NASDAQ",
        date(2020, 1, 1),
        None,
        "sec_xbrl_cover",
        "high",
        "sec://cover/symbol",
        datetime(2020, 1, 1, 20, tzinfo=UTC),
        "historical_interval",
    )
    return security_id, build_security_master((security,), (symbol,))


def _cover_snapshot(tmp_path: Path, *, shares: bool = True, fpi: bool = False):
    security_id, master = _master()
    merge = CoverEvidenceMerge(
        plan_snapshot_id="b" * 64,
        as_of=SIGNAL,
        requested_ciks=(CIK,),
        shard_snapshot_ids=("a" * 64,),
        archives=(),
        share_observations=(
            CoverShareObservation(
                security_id,
                CIK,
                datetime(2026, 5, 1, 20, tzinfo=UTC),
                Decimal("125000000"),
                "sec://cover/shares",
            ),
        )
        if shares
        else (),
        fpi_form_observations=(
            CoverFpiFormObservation(
                CIK,
                "0000000001-20-000001",
                "20-F",
                datetime(2020, 3, 1, 20, tzinfo=UTC),
                "sec://submissions/20-f",
            ),
        )
        if fpi
        else (),
        form_history_proofs=(
            CoverFormHistoryProof(
                CIK,
                SIGNAL,
                ("https://data.sec.gov/submissions/cik1#" + "c" * 64,),
                "sec-submissions-complete://1/" + "d" * 64,
            ),
        ),
        acquisition_gaps=(),
        bootstrap_gaps=(),
        master=master,
    )
    return security_id, materialize_cover_evidence_merge(merge, tmp_path / "cover")


def test_foreign_regime_applies_to_unmapped_line_when_cik_has_other_security() -> None:
    security_id, domestic_master = _master()
    foreign_security = replace(domestic_master.securities[0], domestic_flag=False)
    master = build_security_master((foreign_security,), domestic_master.symbols)
    archive_accession = "0000000001-26-000001"
    foreign_accession = "0000000001-25-000001"
    merge = CoverEvidenceMerge(
        plan_snapshot_id="b" * 64,
        as_of=SIGNAL,
        requested_ciks=(CIK,),
        shard_snapshot_ids=("a" * 64,),
        archives=(CoverArchiveRecord(CIK, archive_accession, "c" * 64, "d" * 64),),
        share_observations=(),
        fpi_form_observations=(
            CoverFpiFormObservation(
                CIK,
                foreign_accession,
                "40-F",
                datetime(2026, 5, 1, 20, tzinfo=UTC),
                "sec://submissions/40-f",
            ),
        ),
        form_history_proofs=(),
        acquisition_gaps=(),
        bootstrap_gaps=(),
        master=master,
    )
    row = FilingDiscoveryRow(
        "OTHER",
        "NYSE",
        "NYSE",
        "Stock",
        "alpha-vantage://listing/1",
        "discovered",
        (CIK,),
    )
    plan = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "e" * 64,
        "f" * 64,
        datetime(2026, 7, 18, tzinfo=UTC),
        (row,),
    )

    regime = _identity_regime_evidence(plan, SimpleNamespace(merge=merge), SIGNAL)

    assert regime.foreign_regime_pointers == {CIK: ("sec://submissions/40-f",)}
    assert security_id in {security.security_id for security in master.securities}


def test_complete_gap_free_cover_history_marks_old_listing_superseded() -> None:
    _security_id, old_master = _master()
    recent_symbol = replace(
        old_master.symbols[0],
        known_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
    )
    master = build_security_master(old_master.securities, (recent_symbol,))
    archives = (
        CoverArchiveRecord(CIK, "0000000001-26-000001", "a" * 64, "b" * 64),
        CoverArchiveRecord(CIK, "0000000001-26-000002", "c" * 64, "d" * 64),
    )
    proof = CoverFormHistoryProof(
        CIK,
        SIGNAL,
        ("https://data.sec.gov/submissions/cik1#" + "e" * 64,),
        "sec-submissions-complete://1/" + "f" * 64,
    )
    merge = CoverEvidenceMerge(
        plan_snapshot_id="1" * 64,
        as_of=SIGNAL,
        requested_ciks=(CIK,),
        shard_snapshot_ids=("2" * 64,),
        archives=archives,
        share_observations=(),
        fpi_form_observations=(),
        form_history_proofs=(proof,),
        acquisition_gaps=(
            CoverAcquisitionGap(
                CIK,
                None,
                "unusable_submission_rows",
                "one malformed legacy feed row",
            ),
        ),
        bootstrap_gaps=(),
        master=master,
    )
    row = FilingDiscoveryRow(
        "OLD",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://listing/old",
        "discovered",
        (CIK,),
        ("sec-fsds://quarter/accession?cik=1#issuer-name",),
    )
    plan = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "3" * 64,
        "4" * 64,
        datetime(2026, 7, 18, tzinfo=UTC),
        (row,),
    )

    regime = _identity_regime_evidence(plan, SimpleNamespace(merge=merge), SIGNAL)

    assert row.listing_evidence_pointer in regime.superseded_pointers_by_listing
    pointers = regime.superseded_pointers_by_listing[row.listing_evidence_pointer]
    assert proof.evidence_pointer in pointers
    assert "sec://cover/symbol" in pointers


def test_complete_terminal_form_history_marks_inactive_listing_superseded() -> None:
    security_id, old_master = _master()
    master = old_master
    terminal = CoverTerminalFormObservation(
        CIK,
        "0000000001-20-000003",
        "15-12B",
        datetime(2020, 2, 1, 20, tzinfo=UTC),
        "sec://submissions/15-12b",
    )
    proof = CoverFormHistoryProof(
        CIK,
        SIGNAL,
        ("https://data.sec.gov/submissions/cik1#" + "e" * 64,),
        "sec-submissions-complete://1/" + "f" * 64,
        (terminal,),
    )
    merge = CoverEvidenceMerge(
        plan_snapshot_id="1" * 64,
        as_of=SIGNAL,
        requested_ciks=(CIK,),
        shard_snapshot_ids=("2" * 64,),
        archives=(),
        share_observations=(),
        fpi_form_observations=(),
        form_history_proofs=(proof,),
        acquisition_gaps=(),
        bootstrap_gaps=(),
        master=master,
    )
    row = FilingDiscoveryRow(
        "OLD",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://listing/old",
        "discovered",
        (CIK,),
        ("sec-fsds://quarter/accession?cik=1#issuer-name",),
    )
    plan = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "3" * 64,
        "4" * 64,
        datetime(2026, 7, 18, tzinfo=UTC),
        (row,),
    )

    regime = _identity_regime_evidence(plan, SimpleNamespace(merge=merge), SIGNAL)

    pointers = regime.superseded_pointers_by_listing[row.listing_evidence_pointer]
    assert terminal.evidence_pointer in pointers
    assert security_id in {security.security_id for security in master.securities}


def test_common_stock_observed_after_terminal_form_is_not_superseded() -> None:
    _security_id, old_master = _master()
    recent_symbol = replace(
        old_master.symbols[0],
        ticker="NEW",
        valid_from=date(2021, 1, 1),
        known_at=datetime(2021, 1, 1, 20, tzinfo=UTC),
    )
    master = build_security_master(old_master.securities, (recent_symbol,))
    terminal = CoverTerminalFormObservation(
        CIK,
        "0000000001-20-000003",
        "15-12B",
        datetime(2020, 2, 1, 20, tzinfo=UTC),
        "sec://submissions/15-12b",
    )
    proof = CoverFormHistoryProof(
        CIK,
        SIGNAL,
        ("https://data.sec.gov/submissions/cik1#" + "e" * 64,),
        "sec-submissions-complete://1/" + "f" * 64,
        (terminal,),
    )
    merge = CoverEvidenceMerge(
        plan_snapshot_id="1" * 64,
        as_of=SIGNAL,
        requested_ciks=(CIK,),
        shard_snapshot_ids=("2" * 64,),
        archives=(),
        share_observations=(),
        fpi_form_observations=(),
        form_history_proofs=(proof,),
        acquisition_gaps=(),
        bootstrap_gaps=(),
        master=master,
    )
    row = FilingDiscoveryRow(
        "OLD",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://listing/old",
        "discovered",
        (CIK,),
        ("sec-fsds://quarter/accession?cik=1#issuer-name",),
    )
    plan = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "3" * 64,
        "4" * 64,
        datetime(2026, 7, 18, tzinfo=UTC),
        (row,),
    )

    regime = _identity_regime_evidence(plan, SimpleNamespace(merge=merge), SIGNAL)

    assert row.listing_evidence_pointer not in regime.superseded_pointers_by_listing


def _sessions() -> tuple[date, ...]:
    calendar = default_calendar()
    output = [date(2026, 7, 17)]
    while len(output) < 21:
        output.append(calendar.previous_session(output[-1]).label)
    return tuple(reversed(output))


def _page(adjustment: str) -> PriceSourcePage:
    body = f'{{"adjustment":"{adjustment}"}}'.encode()
    return PriceSourcePage(
        f"https://data.alpaca.markets/bars?adjustment={adjustment}",
        datetime(2026, 7, 20, 12, tzinfo=UTC),
        hashlib.sha256(body).hexdigest(),
        f"request-{adjustment}",
        body,
    )


def _price_snapshot(tmp_path: Path, security_id: str, *, invalid_adjusted: bool = False):
    bars = tuple(
        VendorDailyBar(
            "ONE",
            datetime(session.year, session.month, session.day, 4, tzinfo=UTC),
            session,
            Decimal("9"),
            Decimal("11"),
            Decimal("8"),
            Decimal("10"),
            1_000_000,
            100,
            Decimal("10"),
            0,
        )
        for session in _sessions()
    )
    query_bounds = (
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 7, 18, tzinfo=UTC),
    )

    def result(adjustment: str) -> PriceFetchResult:
        invalid = invalid_adjusted and adjustment == "all"
        return PriceFetchResult(
            "alpaca",
            "alpaca-v2-stocks-bars-1day-sip-trade-aggregate",
            PriceQuery(("ONE",), *query_bounds, adjustment),  # type: ignore[arg-type]
            (_page(adjustment),),
            () if invalid else bars,
            (
                VendorBarIssue(
                    "ONE",
                    _sessions()[0],
                    "invalid_provider_bar",
                    "zero volume",
                    0,
                ),
            )
            if invalid
            else (),
        )

    binding = PriceSecurityBinding(
        security_id,
        "ONE",
        "NASDAQ",
        date(2020, 1, 1),
        None,
        "sec://cover/symbol",
    )
    return materialize_price_snapshot(
        result("raw"),
        result("all"),
        (binding,),
        tmp_path / "prices",
    )


def test_verified_sources_compose_security_keyed_universe_evidence(tmp_path: Path) -> None:
    security_id, cover = _cover_snapshot(tmp_path)
    prices = _price_snapshot(tmp_path, security_id)
    sic = FilingSicObservation(
        CIK,
        "0000000001-26-000001",
        3571,
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        "sec-fsds://2026q2/hash/filing#sic",
    )

    result = build_security_universe_evidence(
        cover,
        prices,
        (sic,),
        (),
        signal_at=SIGNAL,
    )

    assert not result.gaps and len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.security_id == security_id
    assert len(evidence.price_bars) == 21
    assert evidence.shares_outstanding == Decimal("125000000")
    assert evidence.sic == 3571 and evidence.form_history_complete

    biotech_sic = FilingSicObservation(
        CIK,
        "0000000001-26-000001",
        2834,
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        "sec-fsds://2026q2/hash/filing#sic",
    )
    revenue = TtmFact(
        CIK,
        "revenue",
        date(2025, 3, 31),
        4,
        date(2026, 3, 31),
        "USD",
        Decimal("9999999"),
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        (
            date(2025, 6, 30),
            date(2025, 9, 30),
            date(2025, 12, 31),
            date(2026, 3, 31),
        ),
        ("Revenue",) * 4,
        tuple(f"0000000001-2{year}-000001" for year in (3, 4, 5, 6)),
        (datetime(2026, 5, 1, 20, tzinfo=UTC),) * 4,
        "chain-v1",
    )
    biotech_result = build_security_universe_evidence(
        cover,
        prices,
        (biotech_sic,),
        (revenue,),
        signal_at=SIGNAL,
    )
    assert biotech_result.evidence[0].pre_revenue_biotech is True


def test_invalid_adjusted_provider_bar_quarantines_all_price_evidence(tmp_path: Path) -> None:
    security_id, cover = _cover_snapshot(tmp_path)
    prices = _price_snapshot(tmp_path, security_id, invalid_adjusted=True)
    sic = FilingSicObservation(
        CIK,
        "0000000001-26-000001",
        3571,
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        "sec-fsds://2026q2/hash/filing#sic",
    )

    result = build_security_universe_evidence(cover, prices, (sic,), (), signal_at=SIGNAL)

    assert result.evidence[0].price_bars == ()
    assert any(gap.kind == "invalid_provider_price" for gap in result.gaps)


def test_missing_class_shares_fail_closed_while_fpi_evidence_is_retained(tmp_path: Path) -> None:
    security_id, cover = _cover_snapshot(tmp_path, shares=False, fpi=True)
    prices = _price_snapshot(tmp_path, security_id)
    sic = FilingSicObservation(
        CIK,
        "0000000001-26-000001",
        3571,
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        "sec-fsds://2026q2/hash/filing#sic",
    )

    result = build_security_universe_evidence(
        cover,
        prices,
        (sic,),
        (),
        signal_at=SIGNAL,
    )

    assert result.evidence[0].shares_outstanding is None
    assert [row.form for row in result.evidence[0].form_history] == ["20-F"]
    assert [gap.kind for gap in result.gaps] == ["missing_class_shares"]


def test_filing_sic_reader_requires_hash_verified_fsds_artifact(tmp_path: Path) -> None:
    output = tmp_path / "fsds"
    output.mkdir()
    path = output / "filings.parquet"
    row = {
        "batch_id": "batch-2026q2",
        "source_quarter": "2026q2",
        "source_sha256": "e" * 64,
        "adsh": "0000000001-26-000001",
        "cik": CIK,
        "name": "Issuer One",
        "sic": 3571,
        "countryinc": "US",
        "former": None,
        "changed": None,
        "afs": "4-NON",
        "fye": "1231",
        "form": "10-Q",
        "period": date(2026, 3, 31),
        "fy": 2026,
        "fp": "Q1",
        "filed": date(2026, 5, 1),
        "accepted": datetime(2026, 5, 1, 20, tzinfo=UTC),
        "accepted_raw": "20260501160000",
        "prevrpt": False,
        "instance": "issuer-20260331.xml",
        "nciks": 1,
        "aciks": None,
    }
    pq.write_table(pa.Table.from_pylist([row], schema=FILINGS_SCHEMA), path)
    body_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact = FsdsTableArtifact(
        "filings",
        "filings.parquet",
        1,
        path.stat().st_size,
        body_hash,
        str(FILINGS_SCHEMA),
    )
    result = FsdsIngestResult(
        FsdsQuarter(2026, 2),
        "e" * 64,
        "batch-2026q2",
        output,
        datetime(2026, 7, 1, tzinfo=UTC),
        (artifact,),
        False,
    )

    observations = filing_sic_observations((result,), as_of=SIGNAL)

    assert len(observations) == 1 and observations[0].sic == 3571


def test_price_universe_batches_are_exact_and_reopenable(tmp_path: Path) -> None:
    security_id, cover = _cover_snapshot(tmp_path)
    discovery = FilingDiscoveryPlan(
        SIGNAL.date(),
        "1" * 64,
        "2" * 64,
        datetime(2026, 7, 20, 12, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ONE",
                "NASDAQ",
                "NASDAQ",
                "Stock",
                "alpha-vantage://" + "3" * 64 + "/2",
                "discovered",
                (CIK,),
            ),
        ),
    )

    class FakeProvider(PriceProvider):
        def fetch_daily_bars(self, query: PriceQuery) -> PriceFetchResult:
            page = _page(query.adjustment)
            bars = tuple(
                VendorDailyBar(
                    "ONE",
                    datetime(session.year, session.month, session.day, 4, tzinfo=UTC),
                    session,
                    Decimal("9"),
                    Decimal("11"),
                    Decimal("8"),
                    Decimal("10"),
                    1_000_000,
                    100,
                    Decimal("10"),
                    0,
                )
                for session in _sessions()
            )
            return PriceFetchResult(
                "alpaca",
                "alpaca-v2-stocks-bars-1day-sip-trade-aggregate",
                query,
                (page,),
                bars,
            )

    plan = build_price_universe_plan(
        discovery,
        cover,
        signal_at=SIGNAL,
        batch_size=1,
    )
    created = acquire_price_universe(FakeProvider(), plan, cover, tmp_path / "universe-prices")
    cached = acquire_price_universe(FakeProvider(), plan, cover, tmp_path / "universe-prices")

    assert [row.security_id for row in plan.targets] == [security_id]
    assert created.snapshot_id == cached.snapshot_id
    assert not created.from_cache and cached.from_cache
    assert len(created.price_snapshots) == 1
    assert read_price_universe_snapshot(created.output_dir).plan == plan


def test_price_universe_rebase_allows_discovery_provenance_only_change(
    tmp_path: Path,
) -> None:
    _security_id, old_cover = _cover_snapshot(tmp_path / "old-cover")
    old_discovery = FilingDiscoveryPlan(
        SIGNAL.date(),
        "1" * 64,
        "2" * 64,
        datetime(2026, 7, 20, 12, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ONE",
                "NASDAQ",
                "NASDAQ",
                "Stock",
                "alpha-vantage://" + "3" * 64 + "/2",
                "discovered",
                (CIK,),
            ),
        ),
    )

    class FakeProvider(PriceProvider):
        def fetch_daily_bars(self, query: PriceQuery) -> PriceFetchResult:
            return PriceFetchResult(
                "alpaca",
                "alpaca-v2-stocks-bars-1day-sip-trade-aggregate",
                query,
                (_page(query.adjustment),),
                tuple(
                    VendorDailyBar(
                        "ONE",
                        datetime(session.year, session.month, session.day, 4, tzinfo=UTC),
                        session,
                        Decimal("9"),
                        Decimal("11"),
                        Decimal("8"),
                        Decimal("10"),
                        1_000_000,
                        100,
                        Decimal("10"),
                        0,
                    )
                    for session in _sessions()
                ),
            )

    old_plan = build_price_universe_plan(
        old_discovery,
        old_cover,
        signal_at=SIGNAL,
        batch_size=1,
    )
    source = acquire_price_universe(
        FakeProvider(),
        old_plan,
        old_cover,
        tmp_path / "prices",
    )
    new_discovery = replace(
        old_discovery,
        rows=(
            replace(
                old_discovery.rows[0],
                candidate_evidence_pointers=("sec-fsds://source/accession",),
            ),
        ),
    )
    new_cover = materialize_cover_evidence_merge(
        replace(old_cover.merge, plan_snapshot_id=new_discovery.snapshot_id),
        tmp_path / "new-cover",
    )
    new_plan = build_price_universe_plan(
        new_discovery,
        new_cover,
        signal_at=SIGNAL,
        batch_size=1,
    )

    rebased = rebase_price_universe_snapshot(
        source,
        old_cover,
        new_cover,
        new_plan,
        tmp_path / "prices",
    )

    assert rebased.plan.discovery_snapshot_id == new_discovery.snapshot_id
    assert rebased.plan.targets == source.plan.targets
    assert tuple(row.snapshot_id for row in rebased.price_snapshots) == tuple(
        row.snapshot_id for row in source.price_snapshots
    )


def test_price_plan_rejects_legacy_common_type_when_title_is_a_warrant(tmp_path: Path) -> None:
    _security_id, base_cover = _cover_snapshot(tmp_path / "base")
    warrant_id = mint_security_id(CIK, "sec-cover-class:warrant")
    warrant = Security(
        warrant_id,
        CIK,
        "Warrants to purchase Common Stock",
        "common_stock",
        True,
        "sec-cover-class:warrant",
        "sec_xbrl_cover",
        "sec://cover/warrant",
    )
    warrant_symbol = SymbolInterval(
        warrant_id,
        "ONEW",
        "NASDAQ",
        date(2020, 1, 1),
        None,
        "sec_xbrl_cover",
        "high",
        "sec://cover/warrant-symbol",
        datetime(2020, 1, 1, 20, tzinfo=UTC),
        "historical_interval",
    )
    master = build_security_master(
        (*base_cover.merge.master.securities, warrant),
        (*base_cover.merge.master.symbols, warrant_symbol),
    )
    cover = materialize_cover_evidence_merge(
        replace(base_cover.merge, master=master),
        tmp_path / "extended",
    )
    discovery = FilingDiscoveryPlan(
        SIGNAL.date(),
        "1" * 64,
        "2" * 64,
        datetime(2026, 7, 20, 12, tzinfo=UTC),
        tuple(
            FilingDiscoveryRow(
                ticker,
                "NASDAQ",
                "NASDAQ",
                "Stock",
                f"alpha-vantage://{'3' * 64}/{row_number}",
                "discovered",
                (CIK,),
            )
            for row_number, ticker in enumerate(("ONE", "ONEW"), start=2)
        ),
    )

    plan = build_price_universe_plan(discovery, cover, signal_at=SIGNAL)

    assert [row.ticker for row in plan.targets] == ["ONE"]


def test_phase_2_3_composer_rejects_mixed_input_lineage_before_building() -> None:
    listing = SimpleNamespace(
        snapshot_id="a" * 64,
        as_of=SIGNAL.date(),
        rows=(
            SimpleNamespace(
                state="active",
                exchange="NASDAQ",
                symbol="ONE",
                row_number=2,
                source_sha256="f" * 64,
                asset_type="Stock",
            ),
        ),
    )
    discovery = SimpleNamespace(
        listing_snapshot_id="b" * 64,
        listing_as_of=SIGNAL.date(),
        snapshot_id="c" * 64,
        rows=(
            SimpleNamespace(
                listing_evidence_pointer=f"alpha-vantage://{'e' * 64}/2",
                raw_exchange="NASDAQ",
                asset_type="Stock",
            ),
        ),
    )
    cover = SimpleNamespace(
        snapshot_id="d" * 64,
        merge=SimpleNamespace(as_of=SIGNAL),
    )
    prices = SimpleNamespace(
        plan=SimpleNamespace(
            signal_at=SIGNAL,
            discovery_snapshot_id="c" * 64,
            cover_snapshot_id="d" * 64,
        )
    )
    ingested = (SimpleNamespace(batch_id="batch"),)
    pit = SimpleNamespace(inputs=(SimpleNamespace(batch_id="batch"),))

    with pytest.raises(Phase23BuildError, match="lineage"):
        build_phase_2_3(
            listing,
            discovery,
            cover,
            prices,
            ingested,
            pit,
            signal_at=SIGNAL,
            config=object(),
        )


def test_phase_2_3_rejects_post_cutoff_live_edge_facts(tmp_path: Path) -> None:
    facts_path = tmp_path / "filing_facts.parquet"
    pq.write_table(
        pa.table(
            {
                "accepted": pa.array(
                    [SIGNAL, datetime(2026, 7, 18, 12, tzinfo=UTC)],
                    type=pa.timestamp("us", tz="UTC"),
                )
            }
        ),
        facts_path,
    )
    supplement = SimpleNamespace(output_dir=tmp_path)

    with pytest.raises(Phase23BuildError, match="post-cutoff"):
        _validate_supplements_as_of((supplement,), SIGNAL)


def test_live_edge_filing_header_sic_keeps_acceptance_provenance() -> None:
    accepted = datetime(2026, 5, 1, 20, tzinfo=UTC)
    supplement = SimpleNamespace(
        cik=123,
        accession="0000000123-26-000001",
        accepted=accepted,
        filing_sic=7372,
        filing_sic_evidence_pointer="sec-archive://filing.txt#sic",
    )

    rows = _supplement_sic_observations((supplement,), SIGNAL)

    assert len(rows) == 1
    assert rows[0].sic == 7372
    assert rows[0].accepted == accepted


def test_filing_sic_snapshot_requires_the_exact_cover_lineage() -> None:
    accepted = datetime(2026, 5, 1, 20, tzinfo=UTC)
    record = SimpleNamespace(
        cik=123,
        accession="0000000123-26-000001",
        accepted=accepted,
        sic=7372,
        source_url="https://www.sec.gov/filing.txt",
        source_sha256="a" * 64,
        header_sha256="b" * 64,
        source_kind="header_sic",
    )
    supplement = SimpleNamespace(
        cover_snapshot_id="c" * 64,
        as_of=SIGNAL,
        records=(record,),
    )

    rows = _filing_sic_snapshot_observations((supplement,), SIGNAL, "c" * 64)

    assert rows[0].cik == 123
    assert rows[0].sic == 7372
    with pytest.raises(Phase23BuildError, match="lineage"):
        _filing_sic_snapshot_observations((supplement,), SIGNAL, "d" * 64)


def test_phase_2_3_lineage_allows_same_content_retrieved_at_a_new_instant() -> None:
    listing = SimpleNamespace(
        rows=(
            SimpleNamespace(
                state="active",
                exchange="NASDAQ",
                symbol="ONE",
                row_number=2,
                source_sha256="f" * 64,
                asset_type="Stock",
            ),
            SimpleNamespace(
                state="delisted",
                exchange="NYSE",
                symbol="OLD",
                row_number=2,
                source_sha256="d" * 64,
                asset_type="Stock",
            ),
        )
    )
    discovery = SimpleNamespace(
        rows=(
            SimpleNamespace(
                listing_evidence_pointer=f"alpha-vantage://{'f' * 64}/2",
                raw_exchange="NASDAQ",
                asset_type="Stock",
            ),
        )
    )

    assert _active_listing_matches_discovery(listing, discovery)
