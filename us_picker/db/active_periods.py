"""Survivorship-safe listing membership from ``company_active_periods``.

This is our-architecture replacement for the ``inactive_but_listed_ids``
heuristic in ``portfolio.universes``: it derives point-in-time listing
membership from an explicit interval table instead of a delisting-date /
last-price guess. It plugs into the EXISTING as-of machinery (the criterion
used by both the universe builder and the backtest's ``company_ids`` scoring
path), so no scorer signatures change.

Default-safe: ``active_ids_from_periods`` returns ``None`` when the table is
empty, which tells the caller to fall back to the legacy heuristic. Behavior
is therefore identical to today until the table is seeded
(``bist seed-active-periods``) and the change is A/B-validated. Live scoring
(as_of == today) is unaffected either way, since an inactive company is never
active as-of today.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    CompanyActivePeriod,
    DailyPrice,
    ScoringResult,
)

_FALLBACK_START = date(1900, 1, 1)
# Sources this seeder is allowed to overwrite/widen. A row with any other
# source (e.g. a manual correction) is left untouched.
_AUTO_SOURCES = {
    "delisting_date",
    "current_active",
    "last_price_inferred",
    "last_score_inferred",
    "inactive_no_data",
}


def periods_seeded(session: Session) -> bool:
    """True when the interval table has at least one row."""
    return session.query(CompanyActivePeriod.id).first() is not None


def active_ids_from_periods(session: Session, as_of: date) -> Optional[set[int]]:
    """IDs inactive today but listed at *as_of*, sourced from the interval table.

    Returns ``None`` when the table is unseeded so the caller can use the
    legacy heuristic. Mirrors ``inactive_but_listed_ids`` semantics: only
    names with ``is_active == False`` are returned (active names already match
    the ``is_active`` criterion), so on live dates the result is empty.
    """
    if not periods_seeded(session):
        return None
    rows = (
        session.query(CompanyActivePeriod.company_id)
        .join(Company, Company.id == CompanyActivePeriod.company_id)
        .filter(
            Company.is_active.is_(False),
            CompanyActivePeriod.active_from <= as_of,
            or_(
                CompanyActivePeriod.active_to.is_(None),
                CompanyActivePeriod.active_to >= as_of,
            ),
        )
        .distinct()
    )
    return {cid for (cid,) in rows}


def _observed_ranges(session: Session, model, date_col):
    return {
        row[0]: (row[1], row[2])
        for row in session.query(
            model.company_id,
            func.min(date_col).label("first"),
            func.max(date_col).label("last"),
        ).group_by(model.company_id)
    }


def seed_company_active_periods(session: Session) -> int:
    """Populate/refresh listing intervals from metadata + observed history.

    Priority for ``active_to``: real ``delisting_date`` > still-active >
    last observed price > last observed score > collapsed (no data). Only
    auto-seeded rows are widened on re-run; manual corrections survive.
    Returns the number of inserted or updated rows. Caller commits.
    """
    price_ranges = _observed_ranges(session, DailyPrice, DailyPrice.date)
    score_ranges = _observed_ranges(session, ScoringResult, ScoringResult.scoring_date)
    existing = {
        row.company_id: row for row in session.query(CompanyActivePeriod).all()
    }

    changed = 0
    for company in session.query(Company).all():
        first_price, last_price = price_ranges.get(company.id, (None, None))
        first_score, last_score = score_ranges.get(company.id, (None, None))
        observed_firsts = [d for d in (first_price, first_score) if d is not None]
        first_observed = min(observed_firsts) if observed_firsts else None
        active_from = company.listing_date or first_observed or _FALLBACK_START

        if company.delisting_date is not None:
            active_to, source, confidence, notes = (
                company.delisting_date, "delisting_date", 0.95, None,
            )
        elif bool(company.is_active):
            active_to, source, confidence, notes = (
                None, "current_active", 0.85 if first_price else 0.65, None,
            )
        elif last_price is not None:
            active_to, source, confidence, notes = (
                last_price, "last_price_inferred", 0.70,
                "Inactive, no delisting_date; active_to = last price date.",
            )
        elif last_score is not None:
            active_to, source, confidence, notes = (
                last_score, "last_score_inferred", 0.50,
                "Inactive, no price history; active_to = last score date.",
            )
        else:
            active_to, source, confidence, notes = (
                active_from, "inactive_no_data", 0.35,
                "Inactive with no observed history; interval collapsed.",
            )

        if active_to is not None and active_to < active_from:
            active_from = active_to

        row = existing.get(company.id)
        if row is None:
            session.add(
                CompanyActivePeriod(
                    company_id=company.id,
                    active_from=active_from,
                    active_to=active_to,
                    source=source,
                    confidence=confidence,
                    notes=notes,
                )
            )
            changed += 1
            continue

        # Never touch a manually-corrected row.
        if row.source not in _AUTO_SOURCES:
            continue
        row_changed = False
        if active_from < row.active_from:
            row.active_from = active_from
            row_changed = True
        if row.active_to != active_to and confidence >= (row.confidence or 0.0):
            row.active_to = active_to
            row.source = source
            row.confidence = confidence
            row.notes = notes
            row_changed = True
        if row_changed:
            changed += 1

    if changed:
        session.flush()
    return changed
