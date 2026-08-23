"""D030 applicability-aware fundamental coverage over a date-valid universe."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal

from usinv.data.edgar.securities import MappingResult, SecurityMaster
from usinv.data.edgar.tag_chains import (
    CONCEPT_CHAINS,
    EXCLUDED_FORMS,
    RawFact,
    StandardizedFact,
    Tier,
)

if TYPE_CHECKING:
    from usinv.data.edgar.cover_acquisition import CoverShareObservation

APPLICABILITY_VERSION: Final = "usinv-applicability-v1"
STRUCTURAL_ABSENCE_VERSION: Final = "usinv-structural-absence-v2"
COVER_SHARE_EVIDENCE_VERSION: Final = "usinv-cover-share-evidence-v1"
Classification = Literal["observed", "structural_zero", "not_applicable"]
ProofKind = Literal[
    "direct_fact",
    "fallback_fact",
    "custom_pre",
    "derived_identity",
    "explicit_filing_statement",
    "accounting_identity",
]
CellOutcome = Literal["covered", "uncovered", "not_applicable"]
_OBSERVED_PROOFS: Final = frozenset(
    {"direct_fact", "fallback_fact", "custom_pre", "derived_identity"}
)
_ZERO_PROOFS: Final = frozenset({"direct_fact", "accounting_identity"})
_NOT_APPLICABLE_PROOFS: Final = frozenset({"explicit_filing_statement", "accounting_identity"})


class ApplicabilityError(ValueError):
    """Raised when coverage evidence tries to weaken the D030 contract."""


@dataclass(frozen=True, slots=True)
class UniverseCandidate:
    ticker: str
    exchange: str
    session: date
    market_cap: Decimal | None = None
    eligible: bool = True
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicabilityEvidence:
    cik: int
    concept: str
    classification: Classification
    proof_kind: ProofKind
    value: Decimal | None
    available_from: datetime
    rule_version: str
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class MappingFailure:
    ticker: str
    exchange: str
    session: date
    status: str
    candidate_security_ids: tuple[str, ...]
    issue_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplicabilityCell:
    cik: int
    concept: str
    tier: Tier
    outcome: CellOutcome
    classification: Classification | None
    proof_kind: ProofKind | None
    evidence_pointer: str | None


@dataclass(frozen=True, slots=True)
class ApplicabilityCoverageReport:
    as_of: datetime
    candidates: int
    excluded_candidates: int
    mapped_securities: int
    eligible_issuers: int
    mapping_failures: tuple[MappingFailure, ...]
    cells: tuple[ApplicabilityCell, ...]
    mandatory_missing: tuple[tuple[int, str], ...]
    core_rate: float
    secondary_rate: float
    core_threshold: float = 0.90
    secondary_threshold: float = 0.75
    version: str = APPLICABILITY_VERSION

    @property
    def passed(self) -> bool:
        return (
            not self.mapping_failures
            and not self.mandatory_missing
            and self.core_rate >= self.core_threshold
            and self.secondary_rate >= self.secondary_threshold
        )

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "measurement": "evidence_backed_applicable_cells",
            "missing_value_policy": "missing_is_uncovered_not_zero",
            "as_of": self.as_of.astimezone(UTC).isoformat(),
            "candidates": self.candidates,
            "excluded_candidates": self.excluded_candidates,
            "mapped_securities": self.mapped_securities,
            "eligible_issuers": self.eligible_issuers,
            "core_rate": self.core_rate,
            "secondary_rate": self.secondary_rate,
            "core_threshold": self.core_threshold,
            "secondary_threshold": self.secondary_threshold,
            "passed": self.passed,
            "mandatory_missing": [
                {"cik": cik, "concept": concept} for cik, concept in self.mandatory_missing
            ],
            "mapping_failures": [
                {
                    "ticker": item.ticker,
                    "exchange": item.exchange,
                    "session": item.session.isoformat(),
                    "status": item.status,
                    "candidate_security_ids": item.candidate_security_ids,
                    "issue_ids": item.issue_ids,
                }
                for item in self.mapping_failures
            ],
            "cells": [
                {
                    "cik": item.cik,
                    "concept": item.concept,
                    "tier": item.tier,
                    "outcome": item.outcome,
                    "classification": item.classification,
                    "proof_kind": item.proof_kind,
                    "evidence_pointer": item.evidence_pointer,
                }
                for item in self.cells
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _proof_from_derivation(value: str) -> ProofKind:
    if value.startswith("tag:"):
        return "direct_fact"
    if value.startswith("custom-pre:"):
        return "custom_pre"
    if value.startswith(("difference:", "sum:")):
        return "derived_identity"
    return "fallback_fact"


def observed_standardized_evidence(
    facts: Iterable[StandardizedFact],
) -> tuple[ApplicabilityEvidence, ...]:
    """Convert standardized facts to observed evidence without manufacturing zeros."""
    return tuple(
        ApplicabilityEvidence(
            cik=fact.cik,
            concept=fact.concept,
            classification="observed",
            proof_kind=_proof_from_derivation(fact.derivation),
            value=fact.value,
            available_from=fact.available_from,
            rule_version=fact.chain_version,
            evidence_pointer=(
                f"sec://{fact.cik}/{','.join(fact.source_adshs)}/{','.join(fact.source_tags)}"
            ),
        )
        for fact in facts
    )


_EQUITY_PARENT_TAG: Final = "StockholdersEquity"
_EQUITY_TOTAL_TAG: Final = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
_LIABILITIES_TAG: Final = "Liabilities"
_LIABILITIES_CURRENT_TAG: Final = "LiabilitiesCurrent"
_BALANCE_TOTAL_TAG: Final = "LiabilitiesAndStockholdersEquity"
_OPERATING_INCOME_TAG: Final = "OperatingIncomeLoss"
_TOTAL_EXPENSE_TAG: Final = "CostsAndExpenses"
_OPERATING_EXPENSE_TAG: Final = "OperatingExpenses"
_COST_OF_REVENUE_TAGS: Final = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
)
_GROSS_PROFIT_TAG: Final = "GrossProfit"
_PREFERRED_SHARE_TAGS: Final = frozenset(
    {"PreferredStockSharesIssued", "PreferredStockSharesOutstanding"}
)
_MONETARY_UOM: Final = re.compile(r"[A-Z]{3}")


def _identity_evidence(
    cik: int,
    concept: str,
    classification: Classification,
    proof_kind: ProofKind,
    value: Decimal | None,
    sources: Sequence[RawFact],
) -> ApplicabilityEvidence:
    ordered = tuple(sorted(sources, key=lambda item: (item.accepted, item.adsh, item.tag)))
    return ApplicabilityEvidence(
        cik=cik,
        concept=concept,
        classification=classification,
        proof_kind=proof_kind,
        value=value,
        available_from=max(item.accepted for item in ordered),
        rule_version=STRUCTURAL_ABSENCE_VERSION,
        evidence_pointer=(
            f"sec://{cik}/{','.join(item.adsh for item in ordered)}"
            f"/{','.join(item.tag for item in ordered)}"
        ),
    )


def derive_structural_absence_evidence(
    identity_facts: Iterable[RawFact],
    observed: Iterable[StandardizedFact],
    *,
    as_of: datetime,
) -> tuple[ApplicabilityEvidence, ...]:
    """Derive coverage evidence only from explicit filed accounting identities.

    Emits at most one classification per (cik, concept) using facts accepted at
    or before ``as_of``, so a later filing can never rewrite the evidence a
    signal already used. Missing facts alone never produce evidence.
    """
    if as_of.tzinfo is None:
        raise ApplicabilityError("structural absence cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)

    balance: dict[tuple[int, date, str], dict[str, RawFact]] = defaultdict(dict)
    duration: dict[tuple[int, date, int, str], dict[str, RawFact]] = defaultdict(dict)
    preferred_shares: dict[int, list[RawFact]] = defaultdict(list)
    for fact in identity_facts:
        if fact.accepted.tzinfo is None:
            raise ApplicabilityError("structural evidence requires timezone-aware acceptance")
        if fact.form in EXCLUDED_FORMS or fact.accepted.astimezone(UTC) > cutoff:
            continue
        if fact.tag in _PREFERRED_SHARE_TAGS:
            if fact.qtrs == 0 and fact.uom.lower() in {"shares", "share"}:
                preferred_shares[fact.cik].append(fact)
            continue
        if not _MONETARY_UOM.fullmatch(fact.uom):
            continue
        if fact.qtrs == 0:
            group: dict[str, RawFact] = balance[(fact.cik, fact.ddate, fact.uom)]
        elif 1 <= fact.qtrs <= 4:
            group = duration[(fact.cik, fact.ddate, fact.qtrs, fact.uom)]
        else:
            continue
        current = group.get(fact.tag)
        if current is None or (fact.accepted, fact.adsh) < (current.accepted, current.adsh):
            group[fact.tag] = fact

    observed_cells = {
        (fact.cik, fact.concept)
        for fact in observed
        if fact.available_from.astimezone(UTC) <= cutoff
    }

    minority_observed: dict[int, list[ApplicabilityEvidence]] = defaultdict(list)
    minority_zero: dict[int, list[ApplicabilityEvidence]] = defaultdict(list)
    long_term_debt_zero: dict[int, list[ApplicabilityEvidence]] = defaultdict(list)
    for (cik, _, _), by_tag in sorted(balance.items()):
        parent = by_tag.get(_EQUITY_PARENT_TAG)
        total = by_tag.get(_EQUITY_TOTAL_TAG)
        liabilities = by_tag.get(_LIABILITIES_TAG)
        liabilities_current = by_tag.get(_LIABILITIES_CURRENT_TAG)
        balance_total = by_tag.get(_BALANCE_TOTAL_TAG)
        if parent is not None and total is not None and total.value > parent.value:
            minority_observed[cik].append(
                _identity_evidence(
                    cik,
                    "minority_interest",
                    "observed",
                    "derived_identity",
                    total.value - parent.value,
                    (total, parent),
                )
            )
        # Only the full reported reconciliation proves zero: it excludes both
        # noncontrolling interest and mezzanine items. Equal parent/total
        # equity tags alone cannot rule out redeemable noncontrolling interest.
        if (
            parent is not None
            and liabilities is not None
            and balance_total is not None
            and liabilities.value + parent.value == balance_total.value
        ):
            minority_zero[cik].append(
                _identity_evidence(
                    cik,
                    "minority_interest",
                    "structural_zero",
                    "accounting_identity",
                    Decimal(0),
                    (liabilities, parent, balance_total),
                )
            )
        # Equal total and current liabilities leave zero room for any
        # noncurrent obligation, so every long-term debt component is zero.
        if (
            liabilities is not None
            and liabilities_current is not None
            and liabilities.value == liabilities_current.value
        ):
            long_term_debt_zero[cik].append(
                _identity_evidence(
                    cik,
                    "long_term_debt",
                    "structural_zero",
                    "accounting_identity",
                    Decimal(0),
                    (liabilities, liabilities_current),
                )
            )

    revenue_zero: dict[int, list[ApplicabilityEvidence]] = defaultdict(list)
    for (cik, _, _, _), by_tag in sorted(duration.items()):
        operating_income = by_tag.get(_OPERATING_INCOME_TAG)
        if operating_income is None:
            continue
        # CostsAndExpenses is the total operating deduction from revenue, so
        # OperatingIncomeLoss + CostsAndExpenses == 0 implies zero revenue.
        # OperatingExpenses excludes cost of revenue, so it only proves zero
        # revenue on a single-step statement: if any cost-of-revenue or gross-
        # profit line is present, OperatingIncomeLoss == -OperatingExpenses only
        # forces gross profit to zero (revenue == COGS), not revenue to zero.
        expenses = by_tag.get(_TOTAL_EXPENSE_TAG)
        if expenses is None:
            single_step = not any(tag in by_tag for tag in _COST_OF_REVENUE_TAGS) and (
                _GROSS_PROFIT_TAG not in by_tag
            )
            if single_step:
                expenses = by_tag.get(_OPERATING_EXPENSE_TAG)
        if (
            expenses is not None
            and expenses.value > 0
            and operating_income.value + expenses.value == 0
        ):
            revenue_zero[cik].append(
                _identity_evidence(
                    cik,
                    "revenue",
                    "structural_zero",
                    "accounting_identity",
                    Decimal(0),
                    (operating_income, expenses),
                )
            )

    output: list[ApplicabilityEvidence] = []
    for cik in sorted(set(minority_observed) | set(minority_zero)):
        if (cik, "minority_interest") in observed_cells:
            continue
        output.extend(minority_observed.get(cik) or minority_zero.get(cik) or ())

    for concept, zero_rows in (
        ("long_term_debt", long_term_debt_zero),
        ("revenue", revenue_zero),
    ):
        for cik in sorted(zero_rows):
            if (cik, concept) in observed_cells:
                continue
            output.extend(zero_rows[cik])

    for cik in sorted(preferred_shares):
        if (cik, "preferred_equity") in observed_cells:
            continue
        rows = preferred_shares[cik]
        if any(fact.value != 0 for fact in rows):
            continue
        output.extend(
            _identity_evidence(
                cik,
                "preferred_equity",
                "structural_zero",
                "accounting_identity",
                Decimal(0),
                (fact,),
            )
            for fact in sorted(rows, key=lambda item: (item.accepted, item.adsh, item.tag))
        )

    return tuple(
        sorted(
            output,
            key=lambda item: (item.cik, item.concept, item.available_from, item.evidence_pointer),
        )
    )


def cover_share_evidence(
    observations: Iterable[CoverShareObservation],
    *,
    as_of: datetime,
) -> tuple[ApplicabilityEvidence, ...]:
    """Use filing cover-page share counts as direct shares_outstanding facts."""
    if as_of.tzinfo is None:
        raise ApplicabilityError("cover share evidence cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    return tuple(
        sorted(
            (
                ApplicabilityEvidence(
                    cik=row.cik,
                    concept="shares_outstanding",
                    classification="observed",
                    proof_kind="direct_fact",
                    value=row.shares_outstanding,
                    available_from=row.accepted,
                    rule_version=COVER_SHARE_EVIDENCE_VERSION,
                    evidence_pointer=row.evidence_pointer,
                )
                for row in observations
                if row.accepted.astimezone(UTC) <= cutoff
            ),
            key=lambda item: (item.cik, item.available_from, item.evidence_pointer),
        )
    )


def _validate_evidence(item: ApplicabilityEvidence, concepts: frozenset[str]) -> None:
    if item.cik <= 0 or item.concept not in concepts:
        raise ApplicabilityError("applicability evidence has an unknown issuer/concept")
    if item.available_from.tzinfo is None:
        raise ApplicabilityError("applicability evidence must be timezone-aware")
    if not item.rule_version or not item.evidence_pointer:
        raise ApplicabilityError("applicability evidence requires version and pointer")
    if item.classification == "observed":
        if item.proof_kind not in _OBSERVED_PROOFS or item.value is None:
            raise ApplicabilityError("observed coverage requires a value-bearing fact/derivation")
    elif item.classification == "structural_zero":
        if item.proof_kind not in _ZERO_PROOFS or item.value != 0:
            raise ApplicabilityError(
                "structural zero requires explicit zero or accounting identity"
            )
    elif item.classification == "not_applicable":
        if item.proof_kind not in _NOT_APPLICABLE_PROOFS or item.value is not None:
            raise ApplicabilityError("not-applicable requires explicit non-value filing evidence")
    else:
        raise ApplicabilityError(f"unknown applicability classification: {item.classification}")


def _mapped_issuers(
    master: SecurityMaster,
    candidates: Sequence[UniverseCandidate],
) -> tuple[set[str], set[int], tuple[MappingFailure, ...]]:
    by_security = {item.security_id: item for item in master.securities}
    security_ids: set[str] = set()
    ciks: set[int] = set()
    failures: list[MappingFailure] = []
    for candidate in candidates:
        if not candidate.eligible:
            continue
        result: MappingResult = master.resolve(
            candidate.ticker,
            candidate.exchange,
            candidate.session,
            minimum_confidence="high",
            required_security_type="common_stock",
        )
        if result.status != "mapped" or result.security_id is None:
            failures.append(
                MappingFailure(
                    result.ticker,
                    result.exchange,
                    result.session,
                    result.status,
                    result.candidate_security_ids,
                    result.issue_ids,
                )
            )
            continue
        security = by_security[result.security_id]
        if not security.domestic_flag or security.security_type != "common_stock":
            raise ApplicabilityError("eligible candidate mapped outside the v1 security contract")
        security_ids.add(security.security_id)
        ciks.add(security.cik)
    return security_ids, ciks, tuple(failures)


def build_applicability_coverage(
    master: SecurityMaster,
    candidates: Sequence[UniverseCandidate],
    evidence: Iterable[ApplicabilityEvidence],
    *,
    as_of: datetime,
    mandatory_concepts: Iterable[str],
) -> ApplicabilityCoverageReport:
    """Enforce D030 over mapped date-valid v1 issuers; absence stays uncovered."""
    if as_of.tzinfo is None:
        raise ApplicabilityError("coverage as_of must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    if any(candidate.session > cutoff.date() for candidate in candidates):
        raise ApplicabilityError("universe candidate session is after the coverage cutoff")
    catalog = {chain.concept: chain for chain in CONCEPT_CHAINS}
    concepts = frozenset(catalog)
    mandatory = frozenset(mandatory_concepts)
    if not mandatory <= concepts:
        raise ApplicabilityError("mandatory concept list contains an unknown concept")
    security_ids, ciks, failures = _mapped_issuers(master, candidates)

    by_cell: dict[tuple[int, str], list[ApplicabilityEvidence]] = defaultdict(list)
    for item in evidence:
        _validate_evidence(item, concepts)
        if item.cik in ciks and item.available_from.astimezone(UTC) <= cutoff:
            by_cell[(item.cik, item.concept)].append(item)

    cells: list[ApplicabilityCell] = []
    mandatory_missing: list[tuple[int, str]] = []
    for cik in sorted(ciks):
        for concept, chain in catalog.items():
            candidates_for_cell = by_cell.get((cik, concept), [])
            classifications = {item.classification for item in candidates_for_cell}
            if len(classifications) > 1:
                raise ApplicabilityError(
                    f"conflicting applicability evidence for cik={cik} concept={concept}"
                )
            selected = min(
                candidates_for_cell,
                key=lambda item: (
                    item.available_from,
                    item.rule_version,
                    item.evidence_pointer,
                ),
                default=None,
            )
            if selected is None:
                outcome: CellOutcome = "uncovered"
            elif selected.classification == "not_applicable":
                outcome = "not_applicable"
            else:
                outcome = "covered"
            if concept in mandatory and outcome != "covered":
                mandatory_missing.append((cik, concept))
            cells.append(
                ApplicabilityCell(
                    cik,
                    concept,
                    chain.tier,
                    outcome,
                    selected.classification if selected else None,
                    selected.proof_kind if selected else None,
                    selected.evidence_pointer if selected else None,
                )
            )

    def tier_rate(tier: Tier) -> float:
        applicable = [
            cell for cell in cells if cell.tier == tier and cell.outcome != "not_applicable"
        ]
        if not applicable:
            return 0.0
        return sum(cell.outcome == "covered" for cell in applicable) / len(applicable)

    return ApplicabilityCoverageReport(
        as_of=cutoff,
        candidates=len(candidates),
        excluded_candidates=sum(not item.eligible for item in candidates),
        mapped_securities=len(security_ids),
        eligible_issuers=len(ciks),
        mapping_failures=failures,
        cells=tuple(cells),
        mandatory_missing=tuple(mandatory_missing),
        core_rate=tier_rate("core"),
        secondary_rate=tier_rate("secondary"),
    )
