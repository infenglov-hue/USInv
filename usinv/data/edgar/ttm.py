"""Trailing-twelve-month aggregation over validated fiscal quarters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

from usinv.data.edgar.quarterly import QuarterlyFact


@dataclass(frozen=True, slots=True)
class TtmFact:
    """A four-consecutive-quarter flow with PIT availability provenance."""

    cik: int
    concept: str
    fiscal_year_anchor: date
    fiscal_quarter: int
    period_end: date
    uom: str
    value: Decimal
    available_from: datetime
    quarter_ends: tuple[date, ...]
    source_tags: tuple[str, ...]
    source_adshs: tuple[str, ...]
    source_acceptances: tuple[datetime, ...]
    chain_version: str


def _consecutive(window: Sequence[QuarterlyFact]) -> bool:
    if len(window) != 4:
        return False
    for previous, current in pairwise(window):
        if current.fiscal_quarter != previous.fiscal_quarter % 4 + 1:
            return False
        gap = (current.quarter_end - previous.quarter_end).days
        if not 60 <= gap <= 120:
            return False
    return True


def build_ttm_facts(facts: Iterable[QuarterlyFact]) -> tuple[TtmFact, ...]:
    """Sum only four consecutive duration quarters; instant values are never summed."""
    grouped: dict[tuple[int, str, str], list[QuarterlyFact]] = defaultdict(list)
    for fact in facts:
        if fact.period_kind == "duration":
            grouped[(fact.cik, fact.concept, fact.uom)].append(fact)

    output: list[TtmFact] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda item: item.quarter_end)
        for end_index in range(3, len(rows)):
            window = rows[end_index - 3 : end_index + 1]
            if not _consecutive(window):
                continue
            latest = window[-1]
            evidence = sorted(
                (
                    (tag, adsh, accepted)
                    for quarter in window
                    for tag, adsh, accepted in zip(
                        quarter.source_tags,
                        quarter.source_adshs,
                        quarter.source_acceptances,
                        strict=True,
                    )
                ),
                key=lambda item: (item[2], item[1], item[0]),
            )
            output.append(
                TtmFact(
                    cik=latest.cik,
                    concept=latest.concept,
                    fiscal_year_anchor=latest.fiscal_year_anchor,
                    fiscal_quarter=latest.fiscal_quarter,
                    period_end=latest.quarter_end,
                    uom=latest.uom,
                    value=sum((quarter.value for quarter in window), Decimal(0)),
                    available_from=max(quarter.available_from for quarter in window),
                    quarter_ends=tuple(quarter.quarter_end for quarter in window),
                    source_tags=tuple(item[0] for item in evidence),
                    source_adshs=tuple(item[1] for item in evidence),
                    source_acceptances=tuple(item[2] for item in evidence),
                    chain_version=latest.chain_version,
                )
            )
    return tuple(output)
