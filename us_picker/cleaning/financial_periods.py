"""Point-in-time analytical views for cumulative interim statements.

IsYatirim stores Q1/H1/9M income and cash-flow statements as cumulative
year-to-date values.  Feeding those values directly to annual factor formulas
would make a quarter look like a full year.  This module builds an analytical
view with the following contract:

* BALANCE is a point-in-time stock: use the current period as reported.
* ANNUAL INCOME/CASHFLOW is already a twelve-month flow: use as reported.
* Interim INCOME/CASHFLOW becomes trailing twelve months (TTM):

      current YTD + previous annual - previous-year same YTD

The helpers are deliberately independent from factor scoring.  Both the clean
stage and scorers use the same implementation so a score cannot silently read
a different period basis from ``AdjustedMetric``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from us_picker.db.schema import FinancialStatement


_PERIOD_TYPE_BY_MONTH = {
    3: "Q1",
    6: "Q2",
    9: "Q3",
    12: "ANNUAL",
}
_FLOW_STATEMENT_TYPES = {"INCOME", "CASHFLOW"}


def period_type_for_end(period_end: date) -> Optional[str]:
    """Return the stored period type for a standard calendar period end."""

    return _PERIOD_TYPE_BY_MONTH.get(period_end.month)


def parse_statement_items(statement: object | None) -> Optional[list[dict]]:
    """Parse a statement-like object's JSON payload defensively."""

    payload = getattr(statement, "data_json", None)
    if not payload:
        return None
    try:
        items = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    return items


def _item_key(item: Mapping) -> tuple[str, str, str]:
    """Stable key; real statements have item_code, labels cover odd rows."""

    return (
        str(item.get("item_code") or "").strip(),
        str(item.get("desc_tr") or "").strip(),
        str(item.get("desc_eng") or "").strip(),
    )


def compose_ttm_items(
    current_ytd: list[dict],
    previous_annual: list[dict],
    previous_same_ytd: list[dict],
) -> list[dict]:
    """Create TTM items from cumulative interim statements.

    A value is emitted only when all three source values are numeric.  Missing
    is not treated as zero: doing so would fabricate earnings/cash flow when a
    company changed its reporting layout.
    """

    annual_by_key = {_item_key(item): item for item in previous_annual}
    prior_ytd_by_key = {_item_key(item): item for item in previous_same_ytd}
    result: list[dict] = []

    for current in current_ytd:
        key = _item_key(current)
        annual = annual_by_key.get(key)
        prior_ytd = prior_ytd_by_key.get(key)
        current_value = current.get("value")
        annual_value = annual.get("value") if annual else None
        prior_ytd_value = prior_ytd.get("value") if prior_ytd else None

        value = None
        if all(
            isinstance(v, (int, float)) and not isinstance(v, bool)
            for v in (current_value, annual_value, prior_ytd_value)
        ):
            value = float(current_value) + float(annual_value) - float(prior_ytd_value)

        result.append(
            {
                "item_code": current.get("item_code", ""),
                "desc_tr": current.get("desc_tr", ""),
                "desc_eng": current.get("desc_eng", ""),
                "value": value,
            }
        )

    return result


@dataclass(frozen=True)
class AnalyticalStatement:
    """Small statement-like value object consumed by cleaning and scorers."""

    company_id: int
    period_end: date
    period_type: str
    statement_type: str
    data_json: str
    publication_date: Optional[date]
    calculation_basis: str
    source_statements: tuple[FinancialStatement, ...]
    version: int = 1


def latest_statement_map(
    statements: Iterable[FinancialStatement],
) -> dict[tuple[date, str], FinancialStatement]:
    """Deduplicate revisions to the highest version for each period/type."""

    result: dict[tuple[date, str], FinancialStatement] = {}
    for statement in statements:
        key = (statement.period_end, statement.statement_type)
        existing = result.get(key)
        if existing is None or int(statement.version or 1) > int(existing.version or 1):
            result[key] = statement
    return result


def analytical_statement_from_map(
    statement_map: Mapping[tuple[date, str], FinancialStatement],
    company_id: int,
    period_end: date,
    statement_type: str,
) -> Optional[AnalyticalStatement]:
    """Build the annual/TTM analytical statement for one exact period."""

    period_type = period_type_for_end(period_end)
    if period_type is None:
        return None

    current = statement_map.get((period_end, statement_type))
    current_items = parse_statement_items(current)
    if current is None or current_items is None:
        return None

    sources: tuple[FinancialStatement, ...]
    basis: str
    items: list[dict]

    if statement_type == "BALANCE" or period_type == "ANNUAL":
        sources = (current,)
        basis = "POINT_IN_TIME" if statement_type == "BALANCE" else "ANNUAL"
        items = current_items
    elif statement_type in _FLOW_STATEMENT_TYPES:
        previous_annual_end = date(period_end.year - 1, 12, 31)
        previous_same_end = date(period_end.year - 1, period_end.month, period_end.day)
        previous_annual = statement_map.get((previous_annual_end, statement_type))
        previous_same = statement_map.get((previous_same_end, statement_type))
        previous_annual_items = parse_statement_items(previous_annual)
        previous_same_items = parse_statement_items(previous_same)
        if (
            previous_annual is None
            or previous_same is None
            or previous_annual_items is None
            or previous_same_items is None
        ):
            return None
        sources = (current, previous_annual, previous_same)
        basis = "TTM"
        items = compose_ttm_items(
            current_items,
            previous_annual_items,
            previous_same_items,
        )
    else:
        return None

    publication_dates = [
        statement.publication_date
        for statement in sources
        if statement.publication_date is not None
    ]
    publication_date = max(publication_dates) if publication_dates else None

    return AnalyticalStatement(
        company_id=company_id,
        period_end=period_end,
        period_type=period_type,
        statement_type=statement_type,
        data_json=json.dumps(items, ensure_ascii=False, sort_keys=True),
        publication_date=publication_date,
        calculation_basis=basis,
        source_statements=sources,
        version=max(int(statement.version or 1) for statement in sources),
    )


def load_analytical_statement(
    session: Session,
    company_id: int,
    period_end: date,
    statement_type: str,
) -> Optional[AnalyticalStatement]:
    """DB convenience wrapper for callers without a preloaded context."""

    period_type = period_type_for_end(period_end)
    if period_type is None:
        return None

    required_dates = {period_end}
    if statement_type in _FLOW_STATEMENT_TYPES and period_type != "ANNUAL":
        required_dates.add(date(period_end.year - 1, 12, 31))
        required_dates.add(date(period_end.year - 1, period_end.month, period_end.day))

    rows = (
        session.query(FinancialStatement)
        .filter(
            FinancialStatement.company_id == company_id,
            FinancialStatement.statement_type == statement_type,
            FinancialStatement.period_end.in_(required_dates),
        )
        .all()
    )
    return analytical_statement_from_map(
        latest_statement_map(rows),
        company_id,
        period_end,
        statement_type,
    )


def analytical_input_hash(statements: Iterable[AnalyticalStatement | None]) -> str:
    """Deterministic fingerprint of every raw source used by metric rows."""

    raw_sources: dict[int, FinancialStatement] = {}
    for statement in statements:
        if statement is None:
            continue
        for source in statement.source_statements:
            raw_sources[int(source.id or 0)] = source

    payload = [
        {
            "id": int(source.id or 0),
            "period_end": source.period_end.isoformat(),
            "period_type": source.period_type,
            "statement_type": source.statement_type,
            "version": int(source.version or 1),
            "data_json": source.data_json or "",
        }
        for source in sorted(
            raw_sources.values(),
            key=lambda row: (
                row.period_end,
                row.statement_type,
                int(row.version or 1),
                int(row.id or 0),
            ),
        )
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def analytical_source_periods_json(
    statements: Iterable[AnalyticalStatement | None],
) -> str:
    """Audit manifest describing the raw periods behind an analytical row."""

    manifest: dict[str, list[dict]] = {}
    for statement in statements:
        if statement is None:
            continue
        manifest[statement.statement_type] = [
            {
                "period_end": source.period_end.isoformat(),
                "period_type": source.period_type,
                "version": int(source.version or 1),
            }
            for source in statement.source_statements
        ]
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True)
