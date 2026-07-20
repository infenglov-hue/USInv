from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarDocument, EdgarPayloadError
from usinv.data.edgar.filing_xbrl import FilingFact, FilingParseResult
from usinv.data.edgar.security_bootstrap import (
    CoverFilingEvidence,
    FilingDiscoveryPlan,
    FilingDiscoveryRow,
    build_cover_security_master,
    build_filing_discovery_plan,
    materialize_filing_discovery_plan,
    parse_sec_ticker_associations,
    read_filing_discovery_plan,
    select_cover_filings,
)
from usinv.data.edgar.submissions import SubmissionFeed, SubmissionFiling
from usinv.data.listings import (
    ALPHA_LISTING_HEADER,
    AlphaListingPage,
    AlphaListingQuery,
    AlphaListingSnapshot,
    parse_alpha_listing_page,
)

OBSERVED = datetime(2026, 7, 20, 10, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 17, 20, tzinfo=UTC)


def _document(payload: dict[str, object]) -> EdgarDocument:
    body = repr(payload).encode()
    return EdgarDocument(
        "https://www.sec.gov/files/company_tickers_exchange.json",
        OBSERVED,
        OBSERVED,
        hashlib.sha256(body).hexdigest(),
        payload,
        False,
        False,
    )


def _listing_snapshot() -> AlphaListingSnapshot:
    header = ",".join(ALPHA_LISTING_HEADER) + "\n"
    active = (
        header
        + "AAPL,Apple Inc.,NASDAQ,Stock,1980-12-12,,Active\n"
        + "DUP,Duplicate Corp,NYSE,Stock,2020-01-01,,Active\n"
        + "MOVE,Venue Moved Corp,AMEX,Stock,2020-01-01,,Active\n"
        + "CROSS,Cross Venue Corp,AMEX,Stock,2020-01-01,,Active\n"
        + "ETF,Fund,NASDAQ,ETF,2020-01-01,,Active\n"
    )
    delisted = header + "OLD,Old Corp,NYSE,Stock,2000-01-01,2020-01-01,Delisted\n"
    pages = []
    for state, text in (("active", active), ("delisted", delisted)):
        body = text.encode()
        pages.append(
            AlphaListingPage(
                AlphaListingQuery(date(2026, 7, 17), state),  # type: ignore[arg-type]
                f"https://www.alphavantage.co/query?state={state}&apikey=REDACTED",
                OBSERVED,
                hashlib.sha256(body).hexdigest(),
                body,
            )
        )
    return AlphaListingSnapshot(
        date(2026, 7, 17),
        tuple(pages),
        tuple(row for page in pages for row in parse_alpha_listing_page(page)),
    )


def test_sec_ticker_file_builds_only_an_immutable_discovery_plan(tmp_path: Path) -> None:
    associations = parse_sec_ticker_associations(
        _document(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [
                    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                    [1, "Duplicate One", "DUP", "NYSE"],
                    [2, "Duplicate Two", "DUP", "NYSE"],
                    [10, "Venue Moved Corp", "MOVE", "NYSE"],
                    [11, "Cross Venue One", "CROSS", "NYSE"],
                    [12, "Cross Venue Two", "CROSS", "Nasdaq"],
                    [3, "OTC Corp", "OTC", "OTC"],
                    [4, "No Exchange", "NONE", None],
                    [5, "Unsupported Series", "BC/PB", "NYSE"],
                ],
            }
        )
    )
    plan = build_filing_discovery_plan(_listing_snapshot(), associations)

    rows = {row.ticker: row for row in plan.rows}
    assert rows["AAPL"].status == "discovered" and rows["AAPL"].candidate_ciks == (320193,)
    assert rows["DUP"].status == "ambiguous" and rows["DUP"].candidate_ciks == (1, 2)
    assert rows["MOVE"].status == "discovered" and rows["MOVE"].candidate_ciks == (10,)
    assert rows["CROSS"].status == "ambiguous" and rows["CROSS"].candidate_ciks == (11, 12)
    assert rows["ETF"].status == "unsupported_asset_type"
    assert plan.ciks == (10, 320193)
    assert plan.association_observed_at > CUTOFF
    assert plan.association_unusable_rows == 2
    created = materialize_filing_discovery_plan(plan, tmp_path)
    cached = materialize_filing_discovery_plan(plan, tmp_path)
    assert not created.from_cache and cached.from_cache
    assert read_filing_discovery_plan(created.output_dir) == plan
    created.output_dir.joinpath("discovery.json").write_text("{}", encoding="utf-8")
    with pytest.raises(EdgarPayloadError, match="verification"):
        materialize_filing_discovery_plan(plan, tmp_path)


def test_sec_ticker_schema_and_duplicate_rows_fail_closed() -> None:
    with pytest.raises(EdgarPayloadError, match="fields drifted"):
        parse_sec_ticker_associations(_document({"fields": ["cik"], "data": []}))
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple", "AAPL", "Nasdaq"],
            [320193, "Apple", "AAPL", "Nasdaq"],
        ],
    }
    with pytest.raises(EdgarPayloadError, match="exact duplicate"):
        parse_sec_ticker_associations(_document(payload))

    with pytest.raises(EdgarPayloadError, match="contradicts"):
        FilingDiscoveryPlan(
            date(2026, 7, 17),
            "a" * 64,
            "b" * 64,
            OBSERVED,
            (
                FilingDiscoveryRow(
                    "AAPL",
                    "NASDAQ",
                    "NASDAQ",
                    "Stock",
                    "alpha-vantage://fixture/1",
                    "discovered",
                    (),
                ),
            ),
        )

    unsupported = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "a" * 64,
        "b" * 64,
        OBSERVED,
        (
            FilingDiscoveryRow(
                "BC/PB",
                "NYSE",
                None,
                "Stock",
                "alpha-vantage://fixture/2",
                "unsupported_ticker",
                (),
            ),
        ),
    )
    assert unsupported.ciks == ()


def _filing(cik: int, accession: str, accepted: datetime, ticker: str) -> SubmissionFiling:
    return SubmissionFiling(
        cik,
        accession,
        "10-Q",
        accepted.date(),
        accepted,
        accepted.date(),
        f"{ticker.lower()}-20260331.htm",
        f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        "a" * 64,
    )


def _feed(filings: tuple[SubmissionFiling, ...]) -> SubmissionFeed:
    return SubmissionFeed(
        1,
        "Issuer",
        "operating",
        3571,
        "DE",
        (),
        (),
        filings,
        (),
        OBSERVED,
        "https://data.sec.gov/submissions/CIK0000000001.json",
        "b" * 64,
    )


def _cover_parse(
    filing: SubmissionFiling,
    ticker: str,
    *,
    class_title: str = "Common Stock",
    dimensions: tuple[tuple[str, str], ...] = (),
) -> FilingParseResult:
    def fact(tag: str, value: str) -> FilingFact:
        return FilingFact(
            filing.cik,
            filing.accession,
            tag,
            "dei",
            False,
            "cover",
            None,
            filing.filing_date,
            filing.filing_date,
            0,
            None,
            None,
            None,
            value,
            dimensions,
            filing.accepted,
            filing.form,
            filing.filing_date,
            filing.report_date,
            filing.primary_document,
            f"sec://{filing.accession}/{tag}",
        )

    return FilingParseResult(
        (
            fact("TradingSymbol", ticker),
            fact("SecurityExchangeName", "NASDAQ"),
            fact("Security12bTitle", class_title),
        ),
        (),
    )


def test_cover_filing_selection_and_ticker_change_are_point_in_time() -> None:
    old = _filing(1, "0000000001-25-000001", datetime(2025, 1, 15, 20, tzinfo=UTC), "OLD")
    new = _filing(1, "0000000001-26-000001", datetime(2026, 1, 15, 20, tzinfo=UTC), "NEW")
    future = _filing(
        1,
        "0000000001-26-000002",
        datetime(2026, 7, 18, 20, tzinfo=UTC),
        "FUT",
    )
    selected = select_cover_filings(_feed((old, new, future)), as_of=CUTOFF)
    assert selected == (new, old)

    result = build_cover_security_master(
        (
            CoverFilingEvidence(old, _cover_parse(old, "OLD"), True),
            CoverFilingEvidence(new, _cover_parse(new, "NEW"), True),
        ),
        as_of=CUTOFF,
    )
    master = result.master

    assert result.cover_classes == 2 and not result.gaps
    assert master.resolve("OLD", "NASDAQ", date(2025, 6, 1)).status == "mapped"
    assert master.resolve("OLD", "NASDAQ", date(2026, 6, 1)).status == "unmapped"
    assert master.resolve("NEW", "NASDAQ", date(2026, 6, 1)).status == "mapped"

    with pytest.raises(EdgarPayloadError, match="future filing"):
        build_cover_security_master(
            (CoverFilingEvidence(future, _cover_parse(future, "FUT"), True),),
            as_of=CUTOFF,
        )


def test_cover_bootstrap_keeps_latest_description_for_one_identity_anchor() -> None:
    old = _filing(1, "0000000001-25-000001", datetime(2025, 1, 15, 20, tzinfo=UTC), "ONE")
    new = _filing(1, "0000000001-26-000001", datetime(2026, 1, 15, 20, tzinfo=UTC), "ONE")

    result = build_cover_security_master(
        (
            CoverFilingEvidence(
                old,
                _cover_parse(
                    old,
                    "ONE",
                    class_title="Common Stock",
                    dimensions=(("dei:SecurityAxis", "issuer:CommonStockMember"),),
                ),
                True,
            ),
            CoverFilingEvidence(
                new,
                _cover_parse(
                    new,
                    "ONE",
                    class_title="Common Stock, $0.001 par value per share",
                    dimensions=(("dei:SecurityAxis", "issuer:CommonStockMember"),),
                ),
                True,
            ),
        ),
        as_of=CUTOFF,
    )

    assert len(result.master.securities) == 1
    security = result.master.securities[0]
    assert security.class_title == "Common Stock, $0.001 par value per share"
    assert security.security_type == "common_stock"
    assert result.master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"


def test_cover_selection_reserves_capacity_for_structural_and_event_reports() -> None:
    structural = _filing(
        1,
        "0000000001-26-000001",
        datetime(2026, 4, 1, 20, tzinfo=UTC),
        "ONE",
    )
    events = tuple(
        SubmissionFiling(
            1,
            f"0000000001-26-{index:06d}",
            "8-K",
            date(2026, 5, index),
            datetime(2026, 5, index, 20, tzinfo=UTC),
            None,
            f"event-{index}.htm",
            "sec://submissions",
            "a" * 64,
        )
        for index in range(2, 6)
    )

    selected = select_cover_filings(_feed((structural, *events)), as_of=CUTOFF, maximum=2)

    assert structural in selected
    assert len(selected) == 2 and sum(row.form == "8-K" for row in selected) == 1


def test_cover_bootstrap_records_missing_class_instead_of_guessing() -> None:
    filing = _filing(1, "0000000001-26-000001", datetime(2026, 1, 15, 20, tzinfo=UTC), "ONE")
    result = build_cover_security_master(
        (CoverFilingEvidence(filing, FilingParseResult((), ()), True),),
        as_of=CUTOFF,
    )

    assert not result.master.securities
    assert result.gaps[0].kind == "missing_cover_class"
