from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
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
from usinv.data.tiingo_lifecycle import TiingoLifecycleRow, TiingoLifecycleSnapshot
from usinv.universe import (
    FilingFormObservation,
    IdentityRegimeEvidence,
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


def test_zero_volume_session_is_valid_zero_dollar_liquidity() -> None:
    bar = UniversePriceBar(SESSION, Decimal("10"), 0, "fixture://no-trade")
    assert bar.dollar_volume == 0


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


@pytest.mark.parametrize(
    ("ticker", "name"),
    [
        ("ONEW", "One Corp Warrants"),
        ("ONE-U", "One Acquisition Corp - Units (1 Ord Share & 1/2 War)"),
        ("ONER", "One Acquisition Corp Rights"),
        ("ONE-P-A", "One Corp Depositary Shares Series A"),
        ("ONEN", "One Corp 6.5% Senior Notes Due 2030"),
    ],
)
def test_explicit_non_common_provider_stock_is_not_an_identity_gap(
    ticker: str,
    name: str,
) -> None:
    _master, snapshot = _build(
        _listing_snapshot([(ticker, name, "NASDAQ", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.asset_type_pass and not row.mapping_pass
    assert row.mapping_status == "non_common_listing"
    assert "identity_non_common_listing" in row.exclusion_reasons


def test_explicit_etf_is_not_in_the_common_stock_identity_denominator() -> None:
    snapshot = build_universe_snapshot(
        _listing_snapshot([("FUND", "Example Active ETF", "NASDAQ", "Stock")]),
        build_security_master([], []),
        (),
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
    )

    assert snapshot.rows[0].mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps
    assert not snapshot.identity_mapping_gaps


def test_common_unit_provider_stock_remains_an_identity_candidate() -> None:
    _master, snapshot = _build(
        _listing_snapshot([("ONE", "One Partners LP Common Units", "NASDAQ", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.asset_type_pass and not row.mapping_pass
    assert snapshot.identity_mapping_gaps == (row,)


@pytest.mark.parametrize(
    "ticker",
    ["ONE-P-A", "ONE-P", "ONE-WS", "ONE-UN", "ONE-WD"],
)
def test_nyse_non_common_suffix_is_not_an_identity_gap(ticker: str) -> None:
    _master, snapshot = _build(
        _listing_snapshot([(ticker, "One Corp", "NYSE", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps


@pytest.mark.parametrize(
    "name",
    [
        "Common Shares of Beneficial Interest",
        "Permian Basin Royalty Trust",
        "ProShares Ultra Top QQQ",
        "Innovator Nasdaq100 Managed 10 Buffer",
        "Synthetic Fixed-Income Securities Inc",
        "BBH Trust Select Large Cap",
        "Polen 5Perspectives SmallMid Growth",
        "Amplify Municipal CEF High Income",
        "VOC Energy Trust",
        "NextEra Energy Capital Holdings Inc",
        "Tennessee Valley Authority",
    ],
)
def test_explicit_product_name_is_not_an_identity_gap(name: str) -> None:
    _master, snapshot = _build(
        _listing_snapshot([("PROD", name, "NYSE", "Stock")]),
        [],
        [],
        [],
    )

    assert snapshot.rows[0].mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps


def test_explicit_when_issued_listing_is_not_an_identity_candidate() -> None:
    _master, snapshot = _build(
        _listing_snapshot([("ONE-W", "One Corp When Issued", "NYSE", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps


@pytest.mark.parametrize("ticker", ["BC/PB", "BC/PC"])
def test_nyse_slash_preferred_suffix_is_not_an_identity_gap(ticker: str) -> None:
    _master, snapshot = _build(
        _listing_snapshot([(ticker, "Brunswick Corp", "NYSE", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps


@pytest.mark.parametrize("ticker", ["ALPXR", "AEPPZ", "AAPGV", "KNWND"])
def test_nasdaq_fifth_character_non_common_issue_is_not_an_identity_gap(
    ticker: str,
) -> None:
    _master, snapshot = _build(
        _listing_snapshot([(ticker, "One Corp", "NASDAQ", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps


def test_independent_lifecycle_end_is_diagnostic_and_does_not_weaken_identity_gate() -> None:
    listing = _listing_snapshot([("OLD", "Old Corp", "NASDAQ", "Stock")])
    lifecycle = TiingoLifecycleSnapshot(
        "a" * 64,
        (
            TiingoLifecycleRow(
                "OLD",
                "NASDAQ",
                "Stock",
                "USD",
                date(2020, 1, 1),
                date(2025, 1, 2),
                2,
            ),
        ),
    )
    master = build_security_master([], [])
    snapshot = build_universe_snapshot(
        listing,
        master,
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="c" * 64,
        security_master_snapshot_id="m" * 64,
        lifecycle=lifecycle,
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "unmapped"
    assert snapshot.identity_mapping_gaps == (row,)
    assert any(pointer.startswith("tiingo-supported://") for pointer in row.evidence_pointers)


def test_independent_lifecycle_non_stock_proof_closes_the_identity_gap() -> None:
    listing = _listing_snapshot([("FUND", "Provider Strategy", "NYSE", "Stock")])
    lifecycle = TiingoLifecycleSnapshot(
        "a" * 64,
        (
            TiingoLifecycleRow(
                "FUND",
                "NYSE",
                "ETF",
                "USD",
                date(2020, 1, 1),
                None,
                2,
            ),
        ),
    )
    snapshot = build_universe_snapshot(
        listing,
        build_security_master([], []),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="c" * 64,
        security_master_snapshot_id="m" * 64,
        lifecycle=lifecycle,
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "non_common_listing"
    assert not snapshot.identity_mapping_gaps
    assert any(pointer.startswith("tiingo-supported://") for pointer in row.evidence_pointers)


def test_company_name_containing_preferred_remains_an_identity_candidate() -> None:
    _master, snapshot = _build(
        _listing_snapshot([("PFBC", "Preferred Bank", "NASDAQ", "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "unmapped"
    assert snapshot.identity_mapping_gaps == (row,)


@pytest.mark.parametrize(
    ("ticker", "exchange"),
    [
        ("CTEST", "NYSE"),
        ("MTEST-A", "NYSE"),
        ("NTEST-Z", "NYSE"),
        ("PTEST-W", "NYSE"),
        ("ZZK", "NYSE"),
        ("ZVZZT", "NASDAQ"),
        ("ZXYZ-A", "NASDAQ"),
    ],
)
def test_dedicated_test_symbol_is_evidenced_non_member(
    ticker: str,
    exchange: str,
) -> None:
    _master, snapshot = _build(
        _listing_snapshot([(ticker, "", exchange, "Stock")]),
        [],
        [],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_status == "exchange_test_listing"
    assert not snapshot.identity_mapping_gaps
    assert any(
        "CQS_BINARY_INPUT_SPECIFICATION.pdf" in p or "ERA2016-3" in p
        for p in row.evidence_pointers
    )


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


@pytest.mark.parametrize(
    ("domestic", "security_type", "expected_status"),
    [
        (True, "other", "non_common_listing"),
        (False, "common_stock", "non_domestic_listing"),
    ],
)
def test_collision_entirely_outside_scope_is_not_an_identity_gap(
    domestic: bool,
    security_type: str,
    expected_status: str,
) -> None:
    first = _security(
        1,
        "sec-cover:first",
        domestic=domestic,
        security_type=security_type,
        title="First Instrument" if security_type != "common_stock" else "Class A Common Stock",
    )
    second = _security(
        1,
        "sec-cover:second",
        domestic=domestic,
        security_type=security_type,
        title="Second Instrument" if security_type != "common_stock" else "Common Stock",
    )
    _master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [first, second],
        [_symbol(first, "ONE"), _symbol(second, "ONE")],
        [],
    )

    row = snapshot.rows[0]
    assert not row.mapping_pass and row.mapping_status == expected_status
    assert not snapshot.identity_mapping_gaps


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


@pytest.mark.parametrize(
    ("domestic", "security_type"),
    [(False, "common_stock"), (True, "other")],
)
def test_sector_gate_ignores_mapped_instruments_outside_domestic_common_scope(
    domestic: bool,
    security_type: str,
) -> None:
    security = _security(
        1,
        "sec-cover:excluded",
        domestic=domestic,
        security_type=security_type,
        title="Excluded Instrument",
    )
    _master, snapshot = _build(
        _listing_snapshot([("ONE", "One Corp", "NASDAQ", "Stock")]),
        [security],
        [_symbol(security, "ONE")],
        [],
    )

    row = snapshot.rows[0]
    assert row.mapping_pass and not row.included
    assert not snapshot.sector_mapping_gaps


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


def test_filer_regime_evidence_classifies_unmapped_rows() -> None:
    listings = _listing_snapshot(
        [
            ("FRGN", "Foreign Miner Ltd", "NYSE", "Stock"),
            ("FIPO", "Fresh Ipo Inc", "NASDAQ", "Stock"),
            ("RGAP", "Real Gap Inc", "NASDAQ", "Stock"),
        ]
    )
    active = {row.symbol: row for row in listings.rows if row.state == "active"}

    def pointer(symbol: str) -> str:
        row = active[symbol]
        return f"alpha-vantage://{row.source_sha256}/{row.row_number}"

    regime = IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            pointer("FRGN"): (700,),
            pointer("FIPO"): (800,),
            pointer("RGAP"): (900,),
        },
        foreign_regime_pointers={700: ("sec://700/0000000700-25-000001/20-F",)},
        no_periodic_pointers={800: ("sec-submissions-complete://800/feed",)},
    )
    snapshot = build_universe_snapshot(
        listings,
        build_security_master([], []),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
        regime=regime,
    )

    rows = {row.ticker: row for row in snapshot.rows}
    foreign = rows["FRGN"]
    assert foreign.mapping_status == "non_domestic_listing"
    assert "sec://700/0000000700-25-000001/20-F" in foreign.evidence_pointers
    assert "identity_non_domestic_listing" in foreign.exclusion_reasons
    ipo = rows["FIPO"]
    assert ipo.mapping_status == "no_periodic_filing_at_cutoff"
    assert "sec-submissions-complete://800/feed" in ipo.evidence_pointers
    gap = rows["RGAP"]
    assert gap.mapping_status == "unmapped"

    gap_tickers = {row.ticker for row in snapshot.identity_mapping_gaps}
    assert gap_tickers == {"RGAP"}


def test_regime_evidence_never_reclassifies_mixed_or_unlisted_candidates() -> None:
    listings = _listing_snapshot([("MIXD", "Mixed Evidence Corp", "NYSE", "Stock")])
    row = next(item for item in listings.rows if item.state == "active")
    regime = IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            f"alpha-vantage://{row.source_sha256}/{row.row_number}": (700, 701),
        },
        foreign_regime_pointers={700: ("sec://700/20-F",)},
        no_periodic_pointers={701: ("sec-submissions-complete://701/feed",)},
    )

    snapshot = build_universe_snapshot(
        listings,
        build_security_master([], []),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
        regime=regime,
    )

    assert snapshot.rows[0].mapping_status == "unmapped"
    assert len(snapshot.identity_mapping_gaps) == 1


def test_sec_symbol_interval_proves_listing_was_superseded() -> None:
    listings = _listing_snapshot([("OLD", "Renamed Corp", "NASDAQ", "Stock")])
    row = next(item for item in listings.rows if item.state == "active")
    security = _security(702, "sec-cover:renamed")
    old_symbol = SymbolInterval(
        security.security_id,
        "OLD",
        "NASDAQ",
        date(2025, 1, 1),
        date(2026, 6, 1),
        "sec_xbrl_cover",
        "high",
        "sec://702/old-ticker",
        datetime(2026, 6, 1, 20, tzinfo=UTC),
        "historical_interval",
    )
    current_symbol = _symbol(security, "NEW")
    regime = IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            f"alpha-vantage://{row.source_sha256}/{row.row_number}": (702,),
        },
        foreign_regime_pointers={},
        no_periodic_pointers={},
    )

    snapshot = build_universe_snapshot(
        listings,
        build_security_master([security], [old_symbol, current_symbol]),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
        regime=regime,
    )

    result = snapshot.rows[0]
    assert result.mapping_status == "superseded_sec_listing"
    assert "sec://702/old-ticker" in result.evidence_pointers
    assert not snapshot.identity_mapping_gaps


def test_complete_sec_cover_history_proves_listing_was_superseded() -> None:
    listings = _listing_snapshot([("OLD", "Renamed Corp", "NASDAQ", "Stock")])
    row = next(item for item in listings.rows if item.state == "active")
    regime = IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            f"alpha-vantage://{row.source_sha256}/{row.row_number}": (704,),
        },
        foreign_regime_pointers={},
        no_periodic_pointers={},
        superseded_pointers_by_listing={
            f"alpha-vantage://{row.source_sha256}/{row.row_number}": (
                "sec-history://704/complete",
            ),
        },
    )

    snapshot = build_universe_snapshot(
        listings,
        build_security_master([], []),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
        regime=regime,
    )

    assert snapshot.rows[0].mapping_status == "superseded_sec_listing"
    assert "sec-history://704/complete" in snapshot.rows[0].evidence_pointers
    assert not snapshot.identity_mapping_gaps


def test_future_known_symbol_interval_cannot_supersede_listing() -> None:
    listings = _listing_snapshot([("OLD", "Renamed Corp", "NASDAQ", "Stock")])
    row = next(item for item in listings.rows if item.state == "active")
    security = _security(703, "sec-cover:renamed")
    old_symbol = SymbolInterval(
        security.security_id,
        "OLD",
        "NASDAQ",
        date(2025, 1, 1),
        date(2026, 6, 1),
        "sec_xbrl_cover",
        "high",
        "sec://703/future-ticker-evidence",
        SIGNAL_AT + timedelta(days=1),
        "historical_interval",
    )
    regime = IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            f"alpha-vantage://{row.source_sha256}/{row.row_number}": (703,),
        },
        foreign_regime_pointers={},
        no_periodic_pointers={},
    )

    snapshot = build_universe_snapshot(
        listings,
        build_security_master([security], [old_symbol]),
        [],
        signal_at=SIGNAL_AT,
        config=CONFIG,
        config_hash="a" * 64,
        security_master_snapshot_id="b" * 64,
        regime=regime,
    )

    assert snapshot.rows[0].mapping_status == "unmapped"
    assert len(snapshot.identity_mapping_gaps) == 1
