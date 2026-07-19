"""Fiscal-quarter derivation with fail-closed Q4 quarantine rules."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from usinv.data.edgar.tag_chains import CHAIN_BY_CONCEPT, StandardizedFact

ANNUAL_FORMS: Final = frozenset({"10-K", "10-K/A"})
TRANSITION_FORMS: Final = frozenset({"10-KT", "10-KT/A"})
QUARTERLY_FORMS: Final = frozenset({"10-Q", "10-Q/A"})
QuarterMethod = Literal["direct", "ytd_difference", "annual_difference", "instant"]


@dataclass(frozen=True, slots=True)
class QuarterlyFact:
    """One fiscal-quarter value and the evidence needed to reproduce it."""

    cik: int
    concept: str
    period_kind: str
    fiscal_year_anchor: date
    fiscal_quarter: int
    quarter_end: date
    uom: str
    value: Decimal
    available_from: datetime
    method: QuarterMethod
    source_tags: tuple[str, ...]
    source_adshs: tuple[str, ...]
    source_acceptances: tuple[datetime, ...]
    calendar_anchor_adsh: str
    calendar_anchor_available_from: datetime
    chain_version: str


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    """A withheld annual/quarterly derivation with explicit evidence."""

    cik: int
    concept: str
    fiscal_year_end: date
    uom: str
    reason: str
    detail: str
    available_from: datetime
    source_adshs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuarterlyResult:
    facts: tuple[QuarterlyFact, ...]
    quarantine: tuple[QuarantineRecord, ...]


def _sources(
    facts: Sequence[StandardizedFact],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[datetime, ...]]:
    rows = sorted(
        (
            (tag, adsh, accepted)
            for fact in facts
            for tag, adsh, accepted in zip(
                fact.source_tags,
                fact.source_adshs,
                fact.source_acceptances,
                strict=True,
            )
        ),
        key=lambda item: (item[2], item[1], item[0]),
    )
    return (
        tuple(item[0] for item in rows),
        tuple(item[1] for item in rows),
        tuple(item[2] for item in rows),
    )


def _quarter(
    template: StandardizedFact,
    *,
    fiscal_year_anchor: date,
    calendar_anchor: StandardizedFact,
    fiscal_quarter: int,
    quarter_end: date,
    value: Decimal,
    method: QuarterMethod,
    sources: Sequence[StandardizedFact],
) -> QuarterlyFact:
    tags, adshs, acceptances = _sources(sources)
    return QuarterlyFact(
        cik=template.cik,
        concept=template.concept,
        period_kind=template.period_kind,
        fiscal_year_anchor=fiscal_year_anchor,
        fiscal_quarter=fiscal_quarter,
        quarter_end=quarter_end,
        uom=template.uom,
        value=value,
        available_from=max(*acceptances, calendar_anchor.available_from),
        method=method,
        source_tags=tags,
        source_adshs=adshs,
        source_acceptances=acceptances,
        calendar_anchor_adsh=calendar_anchor.source_adshs[0],
        calendar_anchor_available_from=calendar_anchor.available_from,
        chain_version=template.chain_version,
    )


def _quarantine(
    annual: StandardizedFact,
    reason: str,
    detail: str,
    *sources: StandardizedFact,
) -> QuarantineRecord:
    all_sources = (annual, *sources)
    _, adshs, acceptances = _sources(all_sources)
    return QuarantineRecord(
        cik=annual.cik,
        concept=annual.concept,
        fiscal_year_end=annual.ddate,
        uom=annual.uom,
        reason=reason,
        detail=detail,
        available_from=max(acceptances),
        source_adshs=adshs,
    )


def _window_start(annual: StandardizedFact, previous: StandardizedFact | None) -> date:
    if previous is not None:
        distance = (annual.ddate - previous.ddate).days
        if 330 <= distance <= 385:
            return previous.ddate
    return annual.ddate - timedelta(days=385)


def _one(rows: Sequence[StandardizedFact], qtrs: int) -> StandardizedFact | None:
    matches = [row for row in rows if row.qtrs == qtrs]
    if not matches:
        return None
    return min(matches, key=lambda item: (item.available_from, item.source_adshs))


def _derive_first_three(
    annual: StandardizedFact,
    by_end: dict[date, list[StandardizedFact]],
    *,
    fiscal_year_anchor: date,
    calendar_anchor: StandardizedFact,
    require_three: bool = True,
) -> tuple[list[QuarterlyFact], QuarantineRecord | None]:
    ends = sorted(by_end)
    if (require_three and len(ends) != 3) or len(ends) > 3:
        return [], _quarantine(
            annual,
            "quarter_count_not_three",
            f"expected 3 distinct 10-Q period ends inside fiscal window; found {len(ends)}",
            *(row for end in ends for row in by_end[end]),
        )

    output: list[QuarterlyFact] = []
    for index, end in enumerate(ends, start=1):
        rows = by_end[end]
        direct = _one(rows, 1)
        if direct is not None:
            output.append(
                _quarter(
                    direct,
                    fiscal_year_anchor=fiscal_year_anchor,
                    calendar_anchor=calendar_anchor,
                    fiscal_quarter=index,
                    quarter_end=end,
                    value=direct.value,
                    method="direct",
                    sources=[direct],
                )
            )
            continue
        if index == 1:
            return [], _quarantine(
                annual,
                "missing_q1_direct",
                "Q1 has no qtrs=1 observation; a safe YTD difference is impossible",
                *rows,
            )
        ytd = _one(rows, index)
        if ytd is None:
            return [], _quarantine(
                annual,
                "missing_direct_and_ytd",
                f"Q{index} has neither qtrs=1 nor qtrs={index}",
                *rows,
            )
        if index == 2:
            prior = _one(by_end[ends[0]], 1)
            if prior is None:
                return [], _quarantine(
                    annual,
                    "ytd_inputs_missing",
                    "Q2 YTD fallback has no Q1 source",
                    ytd,
                )
            sources = [ytd, prior]
            value = ytd.value - prior.value
        else:
            prior_ytd = _one(by_end[ends[1]], 2)
            if prior_ytd is not None:
                sources = [ytd, prior_ytd]
                value = ytd.value - prior_ytd.value
            else:
                q1_source = _one(by_end[ends[0]], 1)
                q2_source = _one(by_end[ends[1]], 1)
                if len(output) != 2 or q1_source is None or q2_source is None:
                    return [], _quarantine(
                        annual,
                        "ytd_inputs_missing",
                        "Q3 YTD fallback has incomplete prior quarters",
                        ytd,
                    )
                sources = [ytd, q1_source, q2_source]
                value = ytd.value - output[0].value - output[1].value
        output.append(
            _quarter(
                ytd,
                fiscal_year_anchor=fiscal_year_anchor,
                calendar_anchor=calendar_anchor,
                fiscal_quarter=index,
                quarter_end=end,
                value=value,
                method="ytd_difference",
                sources=sources,
            )
        )
    return output, None


def _derive_duration_year(
    annual: StandardizedFact,
    candidates: Sequence[StandardizedFact],
    previous: StandardizedFact | None,
) -> tuple[list[QuarterlyFact], QuarantineRecord | None]:
    if annual.form in TRANSITION_FORMS:
        return [], _quarantine(
            annual,
            "fiscal_year_change_or_10_kt",
            f"transition form {annual.form} cannot anchor a normal fiscal year",
        )
    if annual.form not in ANNUAL_FORMS:
        return [], None
    if previous is not None and not 330 <= (annual.ddate - previous.ddate).days <= 385:
        return [], _quarantine(
            annual,
            "fiscal_year_change_or_10_kt",
            "consecutive annual period ends are outside the 330-385 day fiscal window",
            previous,
        )
    lower = _window_start(annual, previous)
    rows = [
        row
        for row in candidates
        if row.form in QUARTERLY_FORMS and 1 <= row.qtrs <= 3 and lower < row.ddate < annual.ddate
    ]
    by_end: dict[date, list[StandardizedFact]] = defaultdict(list)
    for row in rows:
        by_end[row.ddate].append(row)
    calendar_anchor = previous or annual
    quarters, issue = _derive_first_three(
        annual,
        by_end,
        fiscal_year_anchor=calendar_anchor.ddate,
        calendar_anchor=calendar_anchor,
    )
    if issue is not None:
        return [], issue

    annual_signature = frozenset(annual.source_tags)
    if any(frozenset(quarter.source_tags) != annual_signature for quarter in quarters):
        return quarters, _quarantine(
            annual,
            "concept_or_source_tag_mismatch",
            "annual and quarterly standardized values do not share the same source-tag set",
            *rows,
        )
    q4_value = annual.value - sum((quarter.value for quarter in quarters), Decimal(0))
    chain = CHAIN_BY_CONCEPT[annual.concept]
    if chain.nonnegative and q4_value < 0:
        return quarters, _quarantine(
            annual,
            "nonnegative_concept_negative",
            f"derived Q4 is negative for nonnegative concept {annual.concept}",
            *rows,
        )
    q4 = _quarter(
        annual,
        fiscal_year_anchor=calendar_anchor.ddate,
        calendar_anchor=calendar_anchor,
        fiscal_quarter=4,
        quarter_end=annual.ddate,
        value=q4_value,
        method="annual_difference",
        sources=[annual, *rows],
    )
    if q4.value + sum((quarter.value for quarter in quarters), Decimal(0)) != annual.value:
        return quarters, _quarantine(
            annual,
            "accounting_identity_failure",
            "Q1+Q2+Q3+Q4 does not exactly reproduce FY",
            *rows,
        )
    return [*quarters, q4], None


def _derive_instant_year(
    annual: StandardizedFact,
    candidates: Sequence[StandardizedFact],
    previous: StandardizedFact | None,
) -> tuple[list[QuarterlyFact], QuarantineRecord | None]:
    if annual.form in TRANSITION_FORMS:
        return [], _quarantine(
            annual,
            "fiscal_year_change_or_10_kt",
            f"transition form {annual.form} cannot anchor a normal fiscal year",
        )
    if annual.form not in ANNUAL_FORMS:
        return [], None
    lower = _window_start(annual, previous)
    rows = [
        row
        for row in candidates
        if row.form in QUARTERLY_FORMS and row.qtrs == 0 and lower < row.ddate < annual.ddate
    ]
    by_end: dict[date, list[StandardizedFact]] = defaultdict(list)
    for row in rows:
        by_end[row.ddate].append(row)
    if len(by_end) != 3:
        return [], _quarantine(
            annual,
            "quarter_count_not_three",
            f"expected 3 instant 10-Q period ends inside fiscal window; found {len(by_end)}",
            *rows,
        )
    calendar_anchor = previous or annual
    output = [
        _quarter(
            _one(by_end[end], 0) or by_end[end][0],
            fiscal_year_anchor=calendar_anchor.ddate,
            calendar_anchor=calendar_anchor,
            fiscal_quarter=index,
            quarter_end=end,
            value=(_one(by_end[end], 0) or by_end[end][0]).value,
            method="instant",
            sources=[_one(by_end[end], 0) or by_end[end][0]],
        )
        for index, end in enumerate(sorted(by_end), start=1)
    ]
    output.append(
        _quarter(
            annual,
            fiscal_year_anchor=calendar_anchor.ddate,
            calendar_anchor=calendar_anchor,
            fiscal_quarter=4,
            quarter_end=annual.ddate,
            value=annual.value,
            method="instant",
            sources=[annual],
        )
    )
    return output, None


def _derive_open_duration(
    anchor: StandardizedFact,
    candidates: Sequence[StandardizedFact],
) -> tuple[list[QuarterlyFact], QuarantineRecord | None]:
    """Derive the open fiscal year's reported 10-Qs using only the prior 10-K anchor."""
    rows = [
        row
        for row in candidates
        if row.form in QUARTERLY_FORMS and 1 <= row.qtrs <= 3 and row.ddate > anchor.ddate
    ]
    by_end: dict[date, list[StandardizedFact]] = defaultdict(list)
    for row in rows:
        by_end[row.ddate].append(row)
    return _derive_first_three(
        anchor,
        by_end,
        fiscal_year_anchor=anchor.ddate,
        calendar_anchor=anchor,
        require_three=False,
    )


def _derive_open_instant(
    anchor: StandardizedFact,
    candidates: Sequence[StandardizedFact],
) -> tuple[list[QuarterlyFact], QuarantineRecord | None]:
    """Label open-year instant facts without consulting a future annual filing."""
    rows = [
        row
        for row in candidates
        if row.form in QUARTERLY_FORMS and row.qtrs == 0 and row.ddate > anchor.ddate
    ]
    by_end: dict[date, list[StandardizedFact]] = defaultdict(list)
    for row in rows:
        by_end[row.ddate].append(row)
    if len(by_end) > 3:
        return [], _quarantine(
            anchor,
            "quarter_count_exceeds_three",
            f"open fiscal year has {len(by_end)} distinct 10-Q period ends",
            *rows,
        )
    output: list[QuarterlyFact] = []
    for index, end in enumerate(sorted(by_end), start=1):
        source = _one(by_end[end], 0) or by_end[end][0]
        output.append(
            _quarter(
                source,
                fiscal_year_anchor=anchor.ddate,
                calendar_anchor=anchor,
                fiscal_quarter=index,
                quarter_end=end,
                value=source.value,
                method="instant",
                sources=[source],
            )
        )
    return output, None


def derive_quarterly_facts(facts: Iterable[StandardizedFact]) -> QuarterlyResult:
    """Derive fiscal quarters from PIT values; invalid Q4s are withheld and counted."""
    grouped: dict[tuple[int, str, str, str], list[StandardizedFact]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.cik, fact.concept, fact.uom, fact.period_kind)].append(fact)

    output: list[QuarterlyFact] = []
    quarantine: list[QuarantineRecord] = []
    for key in sorted(grouped):
        rows = grouped[key]
        period_kind = key[3]
        annual_qtrs = 4 if period_kind == "duration" else 0
        annuals = sorted(
            [
                row
                for row in rows
                if row.qtrs == annual_qtrs and row.form in ANNUAL_FORMS | TRANSITION_FORMS
            ],
            key=lambda item: item.ddate,
        )
        for index, annual in enumerate(annuals):
            previous = annuals[index - 1] if index else None
            if period_kind == "duration":
                derived, issue = _derive_duration_year(annual, rows, previous)
            else:
                derived, issue = _derive_instant_year(annual, rows, previous)
            output.extend(derived)
            if issue is not None:
                quarantine.append(issue)
        if annuals and annuals[-1].form in ANNUAL_FORMS:
            anchor = annuals[-1]
            if period_kind == "duration":
                derived, issue = _derive_open_duration(anchor, rows)
            else:
                derived, issue = _derive_open_instant(anchor, rows)
            output.extend(derived)
            if issue is not None:
                quarantine.append(issue)
    return QuarterlyResult(
        facts=tuple(
            sorted(
                output,
                key=lambda item: (
                    item.cik,
                    item.concept,
                    item.fiscal_year_anchor,
                    item.fiscal_quarter,
                ),
            )
        ),
        quarantine=tuple(
            sorted(
                quarantine,
                key=lambda item: (item.cik, item.concept, item.fiscal_year_end, item.reason),
            )
        ),
    )
