"""Corporate-action price adjustment via band-violation detection.

BIST enforces ~±10% daily price bands, so a close-to-close move beyond ±25%
is not a market move — it is a corporate action (bedelsiz/bedelli/split) the
raw feed does not adjust for. As of 2026-07-09 `corporate_actions` is empty
and IsYatirim's price feed delivers UNADJUSTED closes (`adjusted_close`
arrived byte-equal to `close` on all 1.18M rows), so momentum / technical /
relative-strength read splits as crashes. Quantified damage: BIMAS's ~2:1
bonus issue on 2026-05-14 took its momentum score from 91 → 8.7 and its
composite from 96 → 57 with zero economic change — a top-decile quality name
knocked out of the candidate pool for months. 30 such events since 2023.

Detection guards (avoid adjusting things that are NOT corporate actions):
  * rows within ``ipo_grace_days`` of ``listing_date`` are skipped (IPO price
    discovery has no bands),
  * the previous print must be at most ``max_gap_days`` old (relisting after
    a long suspension can legitimately gap far beyond the band),
  * INDEX / MOCK / TEST company types are skipped.

Adjustment is the classic back-adjustment: for every detected event with
ratio r = close/prev, all rows STRICTLY BEFORE the event date get their
``adjusted_close`` multiplied by r (cumulative across events), recomputed
idempotently from raw ``close`` on every run. ``close`` is never modified.
The observed ratio necessarily absorbs that day's real market move too — a
bounded ±10% error on one day versus a permanent 50-90% cliff.

Consumers pick this up without code changes wherever they already prefer
``adjusted_close`` (momentum, backtest ``_get_price``, selector relative
strength); ``technical.py`` was switched to adjusted-first in the same
change set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy.orm import Session

from us_picker.db.schema import Company, DailyPrice

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

# Company types that never have corporate actions worth adjusting.
_SKIP_COMPANY_TYPES = {"INDEX", "MOCK", "TEST"}

_DEFAULTS = {
    "enabled": False,  # code default OFF; the repo settings.yaml enables it
    "low_ratio": 0.75,
    "high_ratio": 1.30,
    "max_gap_days": 7,
    "ipo_grace_days": 30,
}


@dataclass(frozen=True)
class AdjustConfig:
    enabled: bool
    low_ratio: float
    high_ratio: float
    max_gap_days: int
    ipo_grace_days: int


def load_adjust_config(settings_path: Optional[Path] = None) -> AdjustConfig:
    """Read the price_adjustment block from settings.yaml (defaults if absent)."""
    path = settings_path or _SETTINGS_PATH
    cfg = dict(_DEFAULTS)
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        block = raw.get("price_adjustment") or {}
        for key in cfg:
            if key in block and block[key] is not None:
                cfg[key] = block[key]
    except (OSError, yaml.YAMLError):
        logger.debug("price_adjustment config unavailable; using defaults")
    return AdjustConfig(
        enabled=bool(cfg["enabled"]),
        low_ratio=float(cfg["low_ratio"]),
        high_ratio=float(cfg["high_ratio"]),
        max_gap_days=int(cfg["max_gap_days"]),
        ipo_grace_days=int(cfg["ipo_grace_days"]),
    )


def detect_events(
    rows: list[tuple[date, float]],
    listing_date: Optional[date],
    config: AdjustConfig,
) -> list[tuple[date, float]]:
    """Detect corporate-action events in an ascending (date, close) series.

    Returns [(event_date, ratio)] where ratio = close/prev_close on the day
    the series jumped beyond the band.
    """
    events: list[tuple[date, float]] = []
    grace_until = (
        listing_date + timedelta(days=config.ipo_grace_days) if listing_date else None
    )
    for i in range(1, len(rows)):
        d, close = rows[i]
        prev_d, prev_close = rows[i - 1]
        if not prev_close or prev_close <= 0 or not close or close <= 0:
            continue
        if grace_until is not None and d <= grace_until:
            continue
        if (d - prev_d).days > config.max_gap_days:
            continue  # suspension relisting — a real repricing, not an action
        ratio = close / prev_close
        if ratio < config.low_ratio or ratio > config.high_ratio:
            events.append((d, ratio))
    return events


def adjusted_series(
    rows: list[tuple[date, float]],
    events: list[tuple[date, float]],
) -> list[float]:
    """Back-adjusted closes: rows before each event scaled by its ratio."""
    factors = [1.0] * len(rows)
    for event_date, ratio in events:
        for i, (d, _c) in enumerate(rows):
            if d < event_date:
                factors[i] *= ratio
    return [c * f for (_d, c), f in zip(rows, factors)]


def rebuild_adjusted_closes(
    session: Session,
    company_ids: Optional[list[int]] = None,
    config: Optional[AdjustConfig] = None,
) -> dict:
    """Recompute adjusted_close from raw closes for the whole universe.

    Idempotent: adjusted values are derived from ``close`` each run, so new
    events (yesterday's split) fold in automatically and re-runs are no-ops.
    Only rows whose adjusted value actually changes are written.
    """
    config = config or load_adjust_config()
    stats = {
        "companies_scanned": 0,
        "companies_with_events": 0,
        "events": 0,
        "rows_updated": 0,
    }

    company_query = session.query(Company.id, Company.company_type, Company.listing_date)
    if company_ids:
        company_query = company_query.filter(Company.id.in_(company_ids))

    for cid, ctype, listing in company_query.all():
        if (ctype or "").upper() in _SKIP_COMPANY_TYPES:
            continue
        stats["companies_scanned"] += 1

        price_rows = (
            session.query(DailyPrice.id, DailyPrice.date, DailyPrice.close, DailyPrice.adjusted_close)
            .filter(DailyPrice.company_id == cid, DailyPrice.close.isnot(None))
            .order_by(DailyPrice.date.asc())
            .all()
        )
        if len(price_rows) < 2:
            continue

        series = [(r[1], float(r[2])) for r in price_rows]
        events = detect_events(series, listing, config)
        if not events:
            continue

        stats["companies_with_events"] += 1
        stats["events"] += len(events)

        adjusted = adjusted_series(series, events)
        updates = []
        for (row_id, _d, _close, current_adj), new_adj in zip(price_rows, adjusted):
            current = float(current_adj) if current_adj is not None else None
            if current is None or abs(current - new_adj) > 1e-9:
                updates.append({"id": row_id, "adjusted_close": new_adj})
        if updates:
            session.bulk_update_mappings(DailyPrice, updates)
            stats["rows_updated"] += len(updates)

    session.flush()
    return stats
