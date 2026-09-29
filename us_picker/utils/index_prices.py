"""Helpers for spliced historical price reads.

Some imported BIST price histories contain source scale jumps where one
segment is stored in a different unit. The backtest should not treat those
as real 10x rallies/crashes, so this module computes conservative splice
factors from suspicious consecutive adjusted-close jumps.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import desc
from sqlalchemy.orm import Session

from us_picker.db.schema import Company, DailyPrice

_JUMP_THRESHOLD = 5.0
_SPLICE_CACHE: dict[str, list[tuple[date, float]]] = {}


def clear_index_price_cache() -> None:
    """Clear cached price splice anchors."""
    _SPLICE_CACHE.clear()


def _splice_anchors(session: Session, ticker: str) -> list[tuple[date, float]]:
    """Return pre-jump dates and scale factors for a ticker."""
    cache_key = f"{session.get_bind().url}|{ticker}"
    if cache_key in _SPLICE_CACHE:
        return _SPLICE_CACHE[cache_key]

    rows = (
        session.query(DailyPrice.date, DailyPrice.adjusted_close, DailyPrice.close)
        .join(Company, Company.id == DailyPrice.company_id)
        .filter(Company.ticker == ticker)
        .order_by(DailyPrice.date.asc())
        .all()
    )

    anchors: list[tuple[date, float]] = []
    lower_threshold = 1.0 / _JUMP_THRESHOLD
    previous_date: date | None = None
    previous_value: float | None = None
    for row_date, adjusted_close, close in rows:
        value = adjusted_close if adjusted_close is not None and adjusted_close > 0 else close
        if value is None or value <= 0:
            continue
        value = float(value)
        if previous_date is not None and previous_value is not None:
            ratio = value / previous_value
            if ratio >= _JUMP_THRESHOLD or ratio <= lower_threshold:
                anchors.append((previous_date, ratio))
        previous_date = row_date
        previous_value = value
    _SPLICE_CACHE[cache_key] = anchors
    return anchors


def get_price_splice_factor_by_ticker(
    session: Session,
    ticker: str,
    target_date: date,
) -> float:
    """Return the scale factor that aligns a historical row to latest scale."""
    factor = 1.0
    for anchor_date, anchor_factor in _splice_anchors(session, ticker):
        if target_date <= anchor_date:
            factor *= anchor_factor
    return factor


def get_spliced_price_by_ticker(
    session: Session,
    ticker: str,
    target_date: date,
) -> float:
    """Return latest adjusted close/close after source-scale splicing."""
    row = (
        session.query(DailyPrice.date, DailyPrice.adjusted_close, DailyPrice.close)
        .join(Company, Company.id == DailyPrice.company_id)
        .filter(Company.ticker == ticker, DailyPrice.date <= target_date)
        .order_by(desc(DailyPrice.date))
        .first()
    )
    if not row:
        return 0.0

    row_date, adjusted_close, close = row
    value = adjusted_close if adjusted_close is not None and adjusted_close > 0 else close
    if value is None or value <= 0:
        return 0.0

    return float(value) * get_price_splice_factor_by_ticker(session, ticker, row_date)
