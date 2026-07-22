from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from usinv.data.edgar.applicability import (
    ApplicabilityError,
    UniverseCandidate,
    build_applicability_coverage,
    cover_share_evidence,
    derive_structural_absence_evidence,
)
from usinv.data.edgar.cover_acquisition import CoverShareObservation
from usinv.data.edgar.pit_store import LATEST_FACT_SCHEMA, PIT_FACT_SCHEMA
from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.edgar.tag_chains import (
    RawFact,
    StandardizationError,
    load_structural_identity_facts,
    standardize_facts,
)

AS_OF = datetime(2025, 12, 31, 21, tzinfo=UTC)
ACCEPTED = datetime(2025, 5, 1, 20, tzinfo=UTC)
AFTER_CUTOFF = datetime(2026, 1, 15, 20, tzinfo=UTC)


def _raw(
    cik: int,
    tag: str,
    value: str,
    *,
    uom: str = "USD",
    ddate: date = date(2025, 3, 31),
    qtrs: int = 0,
    accepted: datetime = ACCEPTED,
    adsh: str = "0000000001-25-000001",
    form: str = "10-Q",
) -> RawFact:
    return RawFact(
        cik=cik,
        tag=tag,
        ddate=ddate,
        qtrs=qtrs,
        uom=uom,
        value=Decimal(value),
        accepted=accepted,
        adsh=adsh,
        version="us-gaap/2025",
        form=form,
        filed=accepted.date(),
        filing_period=ddate,
    )


def _security(cik: int, ticker: str) -> tuple[Security, SymbolInterval]:
    anchor = f"cusip:{cik:09d}"
    security_id = mint_security_id(cik, anchor)
    security = Security(
        security_id,
        cik,
        "Common Stock",
        "common_stock",
        True,
        anchor,
        "fixture",
        f"fixture://security/{cik}",
    )
    symbol = SymbolInterval(
        security_id,
        ticker,
        "NASDAQ",
        date(2020, 1, 1),
        None,
        "alpha_vantage_listing_status",
        "high",
        f"fixture://symbol/{ticker}",
        datetime(2026, 1, 1, tzinfo=UTC),
        "historical_interval",
    )
    return security, symbol


def _balance_identity_facts(cik: int) -> list[RawFact]:
    return [
        _raw(cik, "Liabilities", "100"),
        _raw(cik, "StockholdersEquity", "50"),
        _raw(cik, "LiabilitiesAndStockholdersEquity", "150"),
    ]


def test_balance_sheet_identity_proves_zero_minority_interest() -> None:
    evidence = derive_structural_absence_evidence(_balance_identity_facts(1), (), as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.concept == "minority_interest"
    assert item.classification == "structural_zero"
    assert item.proof_kind == "accounting_identity"
    assert item.value == 0
    assert item.available_from == ACCEPTED
    assert "Liabilities" in item.evidence_pointer

    security, symbol = _security(1, "ONE")
    report = build_applicability_coverage(
        build_security_master([security], [symbol]),
        [UniverseCandidate("ONE", "NASDAQ", AS_OF.date())],
        evidence,
        as_of=AS_OF,
        mandatory_concepts=(),
    )
    cell = next(cell for cell in report.cells if cell.concept == "minority_interest")
    assert cell.outcome == "covered" and cell.classification == "structural_zero"


def test_unreconciled_balance_sheet_emits_nothing() -> None:
    facts = [
        _raw(1, "Liabilities", "100"),
        _raw(1, "StockholdersEquity", "50"),
        _raw(1, "LiabilitiesAndStockholdersEquity", "160"),
    ]

    assert derive_structural_absence_evidence(facts, (), as_of=AS_OF) == ()


def test_equity_difference_derives_observed_minority_interest() -> None:
    facts = [
        _raw(1, "StockholdersEquity", "100"),
        _raw(
            1,
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "120",
        ),
    ]

    evidence = derive_structural_absence_evidence(facts, (), as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.concept == "minority_interest"
    assert item.classification == "observed"
    assert item.proof_kind == "derived_identity"
    assert item.value == Decimal("20")


def test_equal_equity_totals_alone_cannot_prove_zero() -> None:
    facts = [
        _raw(1, "StockholdersEquity", "100"),
        _raw(
            1,
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "100",
        ),
    ]

    assert derive_structural_absence_evidence(facts, (), as_of=AS_OF) == ()


def test_positive_difference_outranks_zero_proof_for_one_issuer() -> None:
    facts = [
        *_balance_identity_facts(1),
        _raw(1, "StockholdersEquity", "80", ddate=date(2025, 6, 30)),
        _raw(
            1,
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "95",
            ddate=date(2025, 6, 30),
        ),
    ]

    evidence = derive_structural_absence_evidence(facts, (), as_of=AS_OF)

    assert len(evidence) == 1
    assert evidence[0].classification == "observed"
    assert evidence[0].value == Decimal("15")


def test_zero_preferred_shares_prove_zero_preferred_equity() -> None:
    facts = [_raw(1, "PreferredStockSharesIssued", "0", uom="shares")]

    evidence = derive_structural_absence_evidence(facts, (), as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.concept == "preferred_equity"
    assert item.classification == "structural_zero"
    assert item.proof_kind == "accounting_identity"
    assert item.value == 0


def test_nonzero_preferred_shares_block_zero_evidence() -> None:
    facts = [
        _raw(1, "PreferredStockSharesIssued", "0", uom="shares"),
        _raw(
            1,
            "PreferredStockSharesOutstanding",
            "500",
            uom="shares",
            ddate=date(2025, 6, 30),
        ),
    ]

    assert derive_structural_absence_evidence(facts, (), as_of=AS_OF) == ()


def test_observed_standardized_fact_suppresses_structural_evidence() -> None:
    observed = standardize_facts([_raw(1, "MinorityInterest", "7")])
    assert observed and observed[0].concept == "minority_interest"

    evidence = derive_structural_absence_evidence(
        _balance_identity_facts(1),
        observed,
        as_of=AS_OF,
    )

    assert evidence == ()


def test_future_facts_cannot_create_or_suppress_structural_evidence() -> None:
    future_created = [
        _raw(cik, tag, value, accepted=AFTER_CUTOFF)
        for cik, tag, value in (
            (1, "Liabilities", "100"),
            (1, "StockholdersEquity", "50"),
            (1, "LiabilitiesAndStockholdersEquity", "150"),
        )
    ]
    assert derive_structural_absence_evidence(future_created, (), as_of=AS_OF) == ()

    future_suppressor = [
        _raw(2, "PreferredStockSharesIssued", "0", uom="shares"),
        _raw(
            2,
            "PreferredStockSharesOutstanding",
            "500",
            uom="shares",
            ddate=date(2025, 6, 30),
            accepted=AFTER_CUTOFF,
        ),
    ]
    evidence = derive_structural_absence_evidence(future_suppressor, (), as_of=AS_OF)
    assert len(evidence) == 1 and evidence[0].classification == "structural_zero"

    future_observed = standardize_facts(
        [_raw(3, "MinorityInterest", "7", accepted=AFTER_CUTOFF)]
    )
    evidence = derive_structural_absence_evidence(
        _balance_identity_facts(3),
        future_observed,
        as_of=AS_OF,
    )
    assert len(evidence) == 1 and evidence[0].classification == "structural_zero"


def test_duration_and_foreign_form_facts_are_ignored() -> None:
    facts = [
        _raw(1, "Liabilities", "100", qtrs=4),
        _raw(1, "StockholdersEquity", "50", qtrs=4),
        _raw(1, "LiabilitiesAndStockholdersEquity", "150", qtrs=4),
        _raw(2, "Liabilities", "100", form="20-F"),
        _raw(2, "StockholdersEquity", "50", form="20-F"),
        _raw(2, "LiabilitiesAndStockholdersEquity", "150", form="20-F"),
    ]

    assert derive_structural_absence_evidence(facts, (), as_of=AS_OF) == ()


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ApplicabilityError, match="timezone-aware"):
        derive_structural_absence_evidence(
            _balance_identity_facts(1),
            (),
            as_of=AS_OF.replace(tzinfo=None),
        )

    naive = [
        _raw(1, "Liabilities", "100", accepted=ACCEPTED.replace(tzinfo=None)),
    ]
    with pytest.raises(ApplicabilityError, match="timezone-aware"):
        derive_structural_absence_evidence(naive, (), as_of=AS_OF)


def test_equal_total_and_current_liabilities_prove_zero_long_term_debt() -> None:
    facts = [
        _raw(1, "Liabilities", "80"),
        _raw(1, "LiabilitiesCurrent", "80"),
        _raw(2, "Liabilities", "90"),
        _raw(2, "LiabilitiesCurrent", "80"),
    ]

    evidence = derive_structural_absence_evidence(facts, (), as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.cik == 1
    assert item.concept == "long_term_debt"
    assert item.classification == "structural_zero"
    assert item.proof_kind == "accounting_identity"
    assert item.value == 0


def test_operating_loss_equal_to_expenses_proves_zero_revenue() -> None:
    facts = [
        _raw(1, "OperatingIncomeLoss", "-250", qtrs=1),
        _raw(1, "OperatingExpenses", "250", qtrs=1),
        _raw(2, "OperatingIncomeLoss", "-200", qtrs=1),
        _raw(2, "CostsAndExpenses", "250", qtrs=1),
        _raw(3, "OperatingIncomeLoss", "0", qtrs=1),
        _raw(3, "OperatingExpenses", "0", qtrs=1),
    ]

    evidence = derive_structural_absence_evidence(facts, (), as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.cik == 1
    assert item.concept == "revenue"
    assert item.classification == "structural_zero"
    assert item.proof_kind == "accounting_identity"
    assert item.value == 0

    observed = standardize_facts([_raw(1, "Revenues", "10", qtrs=1)])
    assert (
        derive_structural_absence_evidence(
            [
                _raw(1, "OperatingIncomeLoss", "-250", qtrs=1),
                _raw(1, "OperatingExpenses", "250", qtrs=1),
            ],
            observed,
            as_of=AS_OF,
        )
        == ()
    )


def test_cover_share_observations_become_direct_share_facts() -> None:
    on_time = CoverShareObservation(
        security_id="sec-1",
        cik=1,
        accepted=ACCEPTED,
        shares_outstanding=Decimal("1000"),
        evidence_pointer="sec://1/0000000001-25-000001/cover",
    )
    late = CoverShareObservation(
        security_id="sec-2",
        cik=2,
        accepted=AFTER_CUTOFF,
        shares_outstanding=Decimal("500"),
        evidence_pointer="sec://2/0000000002-26-000001/cover",
    )

    evidence = cover_share_evidence([late, on_time], as_of=AS_OF)

    assert len(evidence) == 1
    item = evidence[0]
    assert item.cik == 1
    assert item.concept == "shares_outstanding"
    assert item.classification == "observed"
    assert item.proof_kind == "direct_fact"
    assert item.value == Decimal("1000")
    assert item.available_from == ACCEPTED

    security, symbol = _security(1, "ONE")
    report = build_applicability_coverage(
        build_security_master([security], [symbol]),
        [UniverseCandidate("ONE", "NASDAQ", AS_OF.date())],
        evidence,
        as_of=AS_OF,
        mandatory_concepts=(),
    )
    cell = next(cell for cell in report.cells if cell.concept == "shares_outstanding")
    assert cell.outcome == "covered" and cell.classification == "observed"

    with pytest.raises(ApplicabilityError, match="timezone-aware"):
        cover_share_evidence([on_time], as_of=AS_OF.replace(tzinfo=None))


def test_revenue_chain_v2_covers_lender_and_utility_top_lines() -> None:
    observed = standardize_facts(
        [
            _raw(1, "RevenuesExcludingInterestAndDividends", "900", qtrs=1),
            _raw(2, "RevenuesNetOfInterestExpense", "800", qtrs=1),
            _raw(3, "RegulatedOperatingRevenue", "700", qtrs=1),
        ]
    )

    assert {(fact.cik, fact.concept) for fact in observed} == {
        (1, "revenue"),
        (2, "revenue"),
        (3, "revenue"),
    }


def _pit_row(fact: RawFact) -> dict[str, object]:
    return {
        "cik": fact.cik,
        "tag": fact.tag,
        "ddate": fact.ddate,
        "qtrs": fact.qtrs,
        "uom": fact.uom,
        "value": fact.value,
        "accepted": fact.accepted,
        "adsh": fact.adsh,
        "version": fact.version,
        "form": fact.form,
        "filed": fact.filed,
        "filing_period": fact.filing_period,
        "filing_fy": 2025,
        "filing_fp": "Q1",
        "footnote": None,
        "batch_id": "fixture",
        "source_quarter": "2025q2",
        "source_sha256": "a" * 64,
    }


def test_loader_reads_only_identity_tags_from_verified_pit(tmp_path: Path) -> None:
    rows = [
        _pit_row(_raw(1, "Liabilities", "100")),
        _pit_row(_raw(1, "StockholdersEquity", "50")),
        _pit_row(_raw(1, "Revenues", "10", qtrs=1)),
        _pit_row(_raw(1, "LiabilitiesAndStockholdersEquity", "150", qtrs=4)),
        _pit_row(_raw(2, "Liabilities", "9", form="20-F")),
    ]
    pit_path = tmp_path / "facts_pit.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=PIT_FACT_SCHEMA), pit_path)

    facts = load_structural_identity_facts(pit_path)

    assert tuple((fact.cik, fact.tag) for fact in facts) == (
        (1, "Liabilities"),
        (1, "LiabilitiesAndStockholdersEquity"),
        (1, "StockholdersEquity"),
    )
    assert all(fact.accepted.tzinfo is not None for fact in facts)

    latest_path = tmp_path / "facts_latest.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=LATEST_FACT_SCHEMA), latest_path)
    with pytest.raises(StandardizationError, match="verified facts_pit"):
        load_structural_identity_facts(latest_path)


def test_40f_filings_are_valid_filer_regime_observations() -> None:
    from usinv.data.edgar.cover_acquisition import CoverFpiFormObservation

    observation = CoverFpiFormObservation(
        cik=700,
        accession="0000000700-25-000001",
        form="40-F",
        accepted=ACCEPTED,
        evidence_pointer="sec://700/0000000700-25-000001/40-F",
    )
    assert observation.form == "40-F"
