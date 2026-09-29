"""Split-normalized share units (US port).

US statements report share counts in whatever split basis was current at the
count's as-of date, while ``daily_prices.close`` is the raw traded price.  A
split between those two dates would make market cap, P/E and EPS growth off by
the split ratio (Nvidia's 10:1 in June 2024 would look 90% cheaper for a
quarter).

Every share count is therefore stored in *base units*::

    shares_base = shares_actual(as_of) / F(as_of)
    price_base(S) = raw_close(S) * F(S)

where ``F(d)`` is the product of every split ratio (new/old) with ex-date on
or before ``d``.  Only splits known by the respective date enter ``F``, so the
normalization is point-in-time safe, and ``price_base * shares_base`` equals
the true market cap at ``S``.  Per-share values computed from base units are
converted back to traded prices with ``/ F(S)`` before anyone sees them.
"""

from __future__ import annotations

import bisect
import threading
import weakref
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from us_picker.db.schema import CorporateAction, DailyPrice

_lock = threading.Lock()
# id(engine) -> (weakref to that engine, company_id -> (ex_dates, cumulative F))
_cache: dict[int, tuple[weakref.ref, dict[int, tuple[list[date], list[float]]]]] = {}


def _load(session: Session) -> dict[int, tuple[list[date], list[float]]]:
    bind = session.get_bind()
    key = id(bind)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0]() is bind:
            return cached[1]
    rows = (
        session.query(
            CorporateAction.company_id,
            CorporateAction.action_date,
            CorporateAction.adjustment_factor,
        )
        .filter(CorporateAction.action_type == "SPLIT")
        .order_by(CorporateAction.company_id, CorporateAction.action_date)
        .all()
    )
    table: dict[int, tuple[list[date], list[float]]] = {}
    for row in rows:
        try:
            company_id, action_date, ratio = row
            ratio = float(ratio)
        except (TypeError, ValueError):
            continue
        if not isinstance(action_date, date) or ratio <= 0:
            continue
        dates, cumulative = table.setdefault(company_id, ([], []))
        previous = cumulative[-1] if cumulative else 1.0
        dates.append(action_date)
        cumulative.append(previous * float(ratio))
    with _lock:
        _cache[key] = (weakref.ref(bind), table)
    return table


def invalidate_split_cache() -> None:
    """Drop cached split tables (call after inserting corporate actions)."""
    with _lock:
        _cache.clear()


def cumulative_split_factor(session: Session, company_id: int, day: date) -> float:
    """``F(day)``: product of split ratios with ex-date on or before ``day``."""
    entry = _load(session).get(company_id)
    if not entry or not isinstance(day, date):
        return 1.0
    dates, cumulative = entry
    idx = bisect.bisect_right(dates, day)
    return cumulative[idx - 1] if idx else 1.0


def split_factor_between(session: Session, company_id: int, after: date, upto: date) -> float:
    """Ratio of splits with ex-date in ``(after, upto]``."""
    if upto <= after:
        return 1.0
    return cumulative_split_factor(session, company_id, upto) / cumulative_split_factor(
        session, company_id, after
    )


def to_base_shares(session: Session, company_id: int, shares: float, as_of: date) -> float:
    return shares / cumulative_split_factor(session, company_id, as_of)


def base_to_traded(session: Session, company_id: int, per_share_base: float, day: date) -> float:
    """Convert a base-unit per-share value into the traded price basis at ``day``."""
    return per_share_base / cumulative_split_factor(session, company_id, day)


def latest_raw_close(
    session: Session, company_id: int, day: Optional[date]
) -> Optional[tuple[date, float]]:
    query = session.query(DailyPrice.date, DailyPrice.close).filter(
        DailyPrice.company_id == company_id,
        DailyPrice.close.isnot(None),
        DailyPrice.close > 0,
    )
    if day is not None:
        query = query.filter(DailyPrice.date <= day)
    row = query.order_by(DailyPrice.date.desc()).first()
    if row is None or row.close is None:
        return None
    return row.date, float(row.close)


def valuation_price(session: Session, company_id: int, day: Optional[date]) -> Optional[float]:
    """Raw close on/before ``day`` expressed in base units (``price_base``)."""
    found = latest_raw_close(session, company_id, day)
    if found is None:
        return None
    price_date, close = found
    return close * cumulative_split_factor(session, company_id, price_date)
