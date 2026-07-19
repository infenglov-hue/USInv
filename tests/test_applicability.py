from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from usinv.data.edgar.applicability import (
    ApplicabilityError,
    ApplicabilityEvidence,
    UniverseCandidate,
    build_applicability_coverage,
)
from usinv.data.edgar.securities import (
    Security,
    SymbolInterval,
    build_security_master,
    mint_security_id,
)
from usinv.data.edgar.tag_chains import CONCEPT_CHAINS

AS_OF = datetime(2025, 12, 31, 21, tzinfo=UTC)


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


def _evidence(
    cik: int,
    concept: str,
    *,
    classification: str = "observed",
    proof_kind: str = "direct_fact",
    value: Decimal | None = Decimal("1"),
    available_from: datetime = datetime(2025, 11, 1, tzinfo=UTC),
) -> ApplicabilityEvidence:
    return ApplicabilityEvidence(
        cik=cik,
        concept=concept,
        classification=classification,
        proof_kind=proof_kind,
        value=value,
        available_from=available_from,
        rule_version="fixture-v1",
        evidence_pointer=f"fixture://{cik}/{concept}/{classification}",
    )


def test_missing_cell_remains_uncovered_and_future_fact_cannot_rescue_it() -> None:
    security, symbol = _security(1, "ONE")
    master = build_security_master([security], [symbol])
    future_revenue = _evidence(
        1,
        "revenue",
        available_from=datetime(2026, 1, 1, tzinfo=UTC),
    )

    report = build_applicability_coverage(
        master,
        [UniverseCandidate("ONE", "NASDAQ", AS_OF.date())],
        [future_revenue],
        as_of=AS_OF,
        mandatory_concepts=["revenue"],
    )

    revenue = next(cell for cell in report.cells if cell.concept == "revenue")
    assert revenue.outcome == "uncovered" and revenue.classification is None
    assert report.mandatory_missing == ((1, "revenue"),)
    assert not report.passed
    assert json.loads(report.to_json())["missing_value_policy"] == ("missing_is_uncovered_not_zero")


def test_explicit_not_applicable_changes_denominator_but_missing_does_not() -> None:
    security, symbol = _security(1, "ONE")
    master = build_security_master([security], [symbol])
    evidence = [
        _evidence(1, chain.concept)
        for chain in CONCEPT_CHAINS
        if chain.concept != "preferred_equity"
    ]
    evidence.append(
        _evidence(
            1,
            "preferred_equity",
            classification="not_applicable",
            proof_kind="explicit_filing_statement",
            value=None,
        )
    )

    report = build_applicability_coverage(
        master,
        [UniverseCandidate("ONE", "NASDAQ", AS_OF.date())],
        evidence,
        as_of=AS_OF,
        mandatory_concepts=["revenue", "net_income"],
    )

    assert report.passed
    assert report.core_rate == 1 and report.secondary_rate == 1
    preferred = next(cell for cell in report.cells if cell.concept == "preferred_equity")
    assert preferred.outcome == "not_applicable"


def test_structural_zero_requires_real_zero_and_allowed_proof() -> None:
    security, symbol = _security(1, "ONE")
    master = build_security_master([security], [symbol])
    candidate = [UniverseCandidate("ONE", "NASDAQ", AS_OF.date())]
    invalid = _evidence(
        1,
        "long_term_debt",
        classification="structural_zero",
        proof_kind="explicit_filing_statement",
        value=None,
    )

    with pytest.raises(ApplicabilityError, match="structural zero"):
        build_applicability_coverage(
            master,
            candidate,
            [invalid],
            as_of=AS_OF,
            mandatory_concepts=(),
        )

    valid = _evidence(
        1,
        "long_term_debt",
        classification="structural_zero",
        proof_kind="accounting_identity",
        value=Decimal(0),
    )
    report = build_applicability_coverage(
        master,
        candidate,
        [valid],
        as_of=AS_OF,
        mandatory_concepts=(),
    )
    debt = next(cell for cell in report.cells if cell.concept == "long_term_debt")
    assert debt.outcome == "covered" and debt.classification == "structural_zero"


def test_mapping_gap_blocks_gate_instead_of_shrinking_issuer_denominator() -> None:
    security, symbol = _security(1, "ONE")
    master = build_security_master([security], [symbol])

    report = build_applicability_coverage(
        master,
        [
            UniverseCandidate("ONE", "NASDAQ", AS_OF.date()),
            UniverseCandidate("MISSING", "NASDAQ", AS_OF.date()),
        ],
        [_evidence(1, chain.concept) for chain in CONCEPT_CHAINS],
        as_of=AS_OF,
        mandatory_concepts=["revenue"],
    )

    assert len(report.mapping_failures) == 1
    assert report.mapping_failures[0].ticker == "MISSING"
    assert not report.passed
