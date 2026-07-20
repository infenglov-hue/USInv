from __future__ import annotations

import hashlib
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
    CoverFormHistoryProof,
    CoverFpiFormObservation,
    CoverShareObservation,
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
)
from usinv.data.universe_evidence import (
    FilingSicObservation,
    build_security_universe_evidence,
    filing_sic_observations,
)
from usinv.phase_2_3 import Phase23BuildError, build_phase_2_3

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


def test_phase_2_3_composer_rejects_mixed_input_lineage_before_building() -> None:
    listing = SimpleNamespace(snapshot_id="a" * 64, as_of=SIGNAL.date())
    discovery = SimpleNamespace(
        listing_snapshot_id="b" * 64,
        listing_as_of=SIGNAL.date(),
        snapshot_id="c" * 64,
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
