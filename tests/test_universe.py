from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.calendar import default_calendar
from usinv.config import load_config
from usinv.data.edgar.applicability import (
    ApplicabilityEvidence,
    build_applicability_coverage,
)
from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.edgar.tag_chains import CONCEPT_CHAINS
from usinv.data.listings import (
    ALPHA_LISTING_HEADER,
    AlphaListingPage,
    AlphaListingQuery,
    AlphaListingSnapshot,
    parse_alpha_listing_page,
)
from usinv.universe import (
    FilingFormObservation,
    SecurityUniverseEvidence,
    UniverseError,
    UniverseGateError,
    UniversePriceBar,
    UniverseStoreError,
    build_universe_snapshot,
    enforce_phase_2_3_gate,
    materialize_universe_snapshot,
    read_universe_snapshot,
)

SESSION = date(2026, 7, 17)
SIGNAL_AT = datetime(2026, 7, 17, 20, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 7, 18, 10, tzinfo=UTC)
EVIDENCE_AT = datetime(2026, 6, 1, 10, tzinfo=UTC)
CONFIG = load_config().universe


def _security(
    cik: int,
    anchor: str,
    *,
    domestic: bool = True,
    security_type: str = "common_stock",
    title: str = "Common Stock",
) -> Security:
    return Security(
        mint_security_id(cik, anchor),
        cik,
        title,
        security_type,
        domestic,
        anchor,
        "fixture",
        f"fixture://security/{cik}/{anchor}",
    )


def _symbol(security: Security, ticker: str) -> SymbolInterval:
    return SymbolInterval(
        security.security_id,
        ticker,
        "NASDAQ",
        date(2020, 1, 1),
        None,
        "alpha_vantage+sec_cover",
        "high",
        f"fixture://symbol/{ticker}",
        OBSERVED_AT,
        "historical_interval",
    )


def _listing_snapshot(rows: list[tuple[str, str, str, str]]) -> AlphaListingSnapshot:
    header = ",".join(ALPHA_LISTING_HEADER) + "\n"
    active_body = header + "".join(
        f"{ticker},{name},{exchange},{asset_type},2020-01-01,,Active\n"
        for ticker, name, exchange, asset_type in rows
    )
    delisted_body = header + "OLD,Old Corp,NYSE,Stock,2000-01-01,2020-01-01,Delisted\n"

    def page(state: str, body: str) -> AlphaListingPage:
        payload = body.encode()
        return AlphaListingPage(
            AlphaListingQuery(SESSION, state),  # type: ignore[arg-type]
            f"https://www.alphavantage.co/query?state={state}&apikey=REDACTED",
            OBSERVED_AT,
            hashlib.sha256(payload).hexdigest(),
            payload,
        )

    pages = (page("active", active_body), page("delisted", delisted_body))
    return AlphaListingSnapshot(
        SESSION,
        pages,
        tuple(row for item in pages for row in parse_alpha_listing_page(item)),
    )


def _bars(*, close: str = "10", volume: int = 200_000) -> tuple[UniversePriceBar, ...]:
    calendar = default_calendar()
    sessions = [SESSION]
    while len(sessions) < CONFIG.dollar_volume_window_sessions:
        sessions.append(calendar.previous_session(sessions[-1]).label)
    return tuple(
        UniversePriceBar(day, Decimal(close), volume, f"fixture://price/{day}")
        for day in reversed(sessions)
    )


def _evidence(
    security: Security,
    *,
    close: str = "10",
    volume: int = 200_000,
    shares: str = "20000000",
    sic: int = 3571,
    forms: tuple[FilingFormObservation, ...] = (),
    pre_revenue_biotech: bool | None = None,
) -> SecurityUniverseEvidence:
    biotech_time = EVIDENCE_AT if pre_revenue_biotech is not None else None
    biotech_pointer = "fixture://biotech" if pre_revenue_biotech is not None else None
    return SecurityUniverseEvidence(
        security.security_id,
        _bars(close=close, volume=volume),
        Decimal(shares),
        EVIDENCE_AT,
        "fixture://shares",
        sic,
        EVIDENCE_AT,
        "fixture://sic",
        forms,
        True,
        "fixture://form-history",
        pre_revenue_biotech,
        biotech_time,
        biotech_pointer,
    )


def _build(
    listings: AlphaListingSnapshot,
    securities: list[Security],
    symbols: list[SymbolInterval],
    evidence: list[SecurityUniverseEvidence],
):
    master = build_security_master(securities, symbols)
    snapshot = build_universe_snapshot(
        listings,
        master,
        evidence,
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
    )
    return master, snapshot


def test_universe_includes_auditable_core_name_and_materializes_immutably(
    tmp_path: Path,
) -> None:
    security = _security(1, "cusip:one")
    master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "ONE")],
        [_evidence(security)],
    )

    row = snapshot.rows[0]
    assert row.included and row.size_bucket == "core"
    assert row.raw_close == Decimal("10")
    assert row.median_dollar_volume == Decimal("2000000")
    assert row.class_market_cap == Decimal("200000000")
    assert row.ff12_label == "BusEq" and row.ff49_label == "Hardw"
    assert row.hygiene_status == "phase-2.3-pass-through-v1"
    assert snapshot.applicability_candidates()[0].ticker == "ONE"

    created = materialize_universe_snapshot(snapshot, tmp_path)
    cached = materialize_universe_snapshot(snapshot, tmp_path)
    assert not created.from_cache and cached.from_cache
    reopened = read_universe_snapshot(created.output_dir)
    assert reopened == snapshot
    created.output_dir.joinpath("universe_snapshots.parquet").write_bytes(b"tampered")
    with pytest.raises(UniverseStoreError, match="verification"):
        materialize_universe_snapshot(snapshot, tmp_path)
    assert master.resolve("ONE", "NASDAQ", SESSION).security_id == security.security_id


def test_future_price_or_filing_evidence_is_rejected_instead_of_filtered() -> None:
    security = _security(1, "cusip:one")
    future_bar = UniversePriceBar(date(2026, 7, 20), Decimal("10"), 1, "fixture://future")
    evidence = _evidence(security)
    future_prices = SecurityUniverseEvidence(
        evidence.security_id,
        (*evidence.price_bars, future_bar),
        evidence.shares_outstanding,
        evidence.shares_available_from,
        evidence.shares_evidence_pointer,
        evidence.sic,
        evidence.sic_available_from,
        evidence.sic_evidence_pointer,
    )
    with pytest.raises(UniverseError, match="future raw price"):
        _build(
            _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
            [security],
            [_symbol(security, "ONE")],
            [future_prices],
        )

    future_filing = FilingFormObservation(
        "20-F", datetime(2026, 7, 18, 11, tzinfo=UTC), "fixture://future-filing"
    )
    with pytest.raises(UniverseError, match="future filing"):
        _build(
            _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
            [security],
            [_symbol(security, "ONE")],
            [_evidence(security, forms=(future_filing,))],
        )


def test_raw_price_floor_is_not_rewritten_by_any_adjusted_series() -> None:
    security = _security(1, "cusip:penny")
    _master, snapshot = _build(
        _listing_snapshot([("PEN", "Penny Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "PEN")],
        [_evidence(security, close="1.20", volume=2_000_000)],
    )

    row = snapshot.rows[0]
    assert not row.raw_close_pass and not row.included
    assert "raw_close_missing_or_not_above_floor" in row.exclusion_reasons


def test_fpi_financial_and_pre_revenue_biotech_are_explicit_exclusions() -> None:
    fpi = _security(1, "cusip:fpi")
    bank = _security(2, "cusip:bank")
    biotech = _security(3, "cusip:bio")
    fpi_form = FilingFormObservation("20-F", EVIDENCE_AT, "fixture://20f")
    _master, snapshot = _build(
        _listing_snapshot(
            [
                ("FPI", "Foreign Corp", "NASDAQ", "Stock"),
                ("BANK", "Bank Corp", "NASDAQ", "Stock"),
                ("BIO", "Bio Corp", "NASDAQ", "Stock"),
            ]
        ),
        [fpi, bank, biotech],
        [_symbol(fpi, "FPI"), _symbol(bank, "BANK"), _symbol(biotech, "BIO")],
        [
            _evidence(fpi, forms=(fpi_form,)),
            _evidence(bank, sic=6021),
            _evidence(biotech, sic=2834, pre_revenue_biotech=True),
        ],
    )

    rows = {row.ticker: row for row in snapshot.rows}
    assert not rows["FPI"].fpi_pass
    assert not rows["BANK"].sector_pass and rows["BANK"].ff49_label == "Banks"
    assert not rows["BIO"].sector_pass
    assert not snapshot.included


def test_biotech_carveout_uses_only_the_registered_sics_and_requires_evidence() -> None:
    research = _security(1, "cusip:research")
    pharma = _security(2, "cusip:pharma")
    _master, snapshot = _build(
        _listing_snapshot(
            [
                ("RND", "Research Corp", "NASDAQ", "Stock"),
                ("PHA", "Pharma Corp", "NASDAQ", "Stock"),
            ]
        ),
        [research, pharma],
        [_symbol(research, "RND"), _symbol(pharma, "PHA")],
        [
            _evidence(research, sic=8731),
            _evidence(pharma, sic=2835),
        ],
    )

    rows = {row.ticker: row for row in snapshot.rows}
    assert not rows["RND"].sector_pass
    assert rows["PHA"].sector_pass


def test_only_most_liquid_class_is_admitted_but_issuer_cap_aggregates_classes() -> None:
    first = _security(10, "cusip:first")
    second = _security(10, "cusip:second")
    _master, snapshot = _build(
        _listing_snapshot(
            [
                ("CLS.A", "Class A", "NASDAQ", "Stock"),
                ("CLS.B", "Class B", "NASDAQ", "Stock"),
            ]
        ),
        [first, second],
        [_symbol(first, "CLS.A"), _symbol(second, "CLS.B")],
        [
            _evidence(first, volume=200_000, shares="6000000"),
            _evidence(second, volume=400_000, shares="5000000"),
        ],
    )

    rows = {row.ticker: row for row in snapshot.rows}
    assert not rows["CLS.A"].primary_line_pass
    assert rows["CLS.B"].included
    assert rows["CLS.B"].issuer_market_cap == Decimal("110000000")
    assert len(snapshot.included) == 1


def test_large_company_can_enter_the_separate_large_cap_bucket() -> None:
    security = _security(1, "cusip:large")
    _master, snapshot = _build(
        _listing_snapshot([("BIG", "Big Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "BIG")],
        [_evidence(security, close="100", volume=100_000, shares="200000000")],
    )

    assert snapshot.rows[0].included
    assert snapshot.rows[0].size_bucket == "large_cap"


def _full_coverage(master, snapshot):
    cik = snapshot.included[0].cik
    assert cik is not None
    evidence = [
        ApplicabilityEvidence(
            cik,
            chain.concept,
            "observed",
            "direct_fact",
            Decimal("1"),
            EVIDENCE_AT,
            "fixture-v1",
            f"fixture://coverage/{chain.concept}",
        )
        for chain in CONCEPT_CHAINS
    ]
    return build_applicability_coverage(
        master,
        snapshot.applicability_candidates(),
        evidence,
        as_of=SIGNAL_AT,
        mandatory_concepts=("revenue", "net_income"),
    )


def test_phase_gate_passes_only_with_exact_final_denominator_and_full_coverage() -> None:
    security = _security(1, "cusip:one")
    master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "ONE")],
        [_evidence(security)],
    )
    coverage = _full_coverage(master, snapshot)

    enforce_phase_2_3_gate(snapshot, coverage)
    assert coverage.core_rate == 1 and coverage.secondary_rate == 1


def test_phase_gate_blocks_any_approved_exchange_identity_gap() -> None:
    security = _security(1, "cusip:one")
    master, snapshot = _build(
        _listing_snapshot(
            [
                ("ONE", "One Corp", "NASDAQ", "Stock"),
                ("MISSING", "Missing Corp", "NASDAQ", "Stock"),
            ]
        ),
        [security],
        [_symbol(security, "ONE")],
        [_evidence(security)],
    )
    coverage = _full_coverage(master, snapshot)

    with pytest.raises(UniverseGateError, match="identity mappings"):
        enforce_phase_2_3_gate(snapshot, coverage)

    missing = next(row for row in snapshot.rows if row.ticker == "MISSING")
    assert "identity_unmapped" in missing.exclusion_reasons


def test_non_common_cover_mapping_is_an_exclusion_not_an_identity_gap() -> None:
    warrant = _security(
        1,
        "sec-cover:warrant",
        security_type="other",
        title="Redeemable Warrant",
    )
    _master, snapshot = _build(
        _listing_snapshot([("ONEW", "One Corp Warrants", "NASDAQ", "Stock")]),
        [warrant],
        [_symbol(warrant, "ONEW")],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_pass and not row.common_stock_pass
    assert "not_common_stock" in row.exclusion_reasons
    assert not snapshot.identity_mapping_gaps


def test_common_cover_wins_only_when_same_symbol_non_equity_causes_collision() -> None:
    common = _security(1, "sec-cover:common")
    note = _security(1, "sec-cover:note", security_type="other", title="3.125% Notes")
    _master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [common, note],
        [_symbol(common, "ONE"), _symbol(note, "ONE")],
        [_evidence(common)],
    )

    row = snapshot.rows[0]
    assert row.mapping_pass and row.security_id == common.security_id
    assert row.included and not snapshot.identity_mapping_gaps


def test_missing_form_history_and_sic_are_quarantined_not_assumed_safe() -> None:
    security = _security(1, "cusip:one")
    incomplete = replace(
        _evidence(security),
        sic=None,
        sic_available_from=None,
        sic_evidence_pointer=None,
        form_history_complete=False,
        form_history_evidence_pointer=None,
    )
    _master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "ONE")],
        [incomplete],
    )

    row = snapshot.rows[0]
    assert not row.fpi_pass and not row.sector_pass and not row.included
    assert snapshot.sector_mapping_gaps == (row,)


def test_signal_timestamp_must_be_the_official_session_close() -> None:
    security = _security(1, "cusip:one")
    master = build_security_master([security], [_symbol(security, "ONE")])

    with pytest.raises(UniverseError, match="official XNYS close"):
        build_universe_snapshot(
            _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
            master,
            [_evidence(security)],
            signal_at=datetime(2026, 7, 17, 19, 59, tzinfo=UTC),
            config=CONFIG,
            config_hash="a" * 64,
            security_master_snapshot_id="b" * 64,
        )
