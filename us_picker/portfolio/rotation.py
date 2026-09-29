"""Rotation-cadence calendar shared by the CLI pick guard, Telegram
notifications and the mobile feed manifest.

The portfolio rotates every ``selection.rotation_weeks`` weeks on Mondays,
with cycle parity anchored at ``selection.rotation_anchor_monday``
(a known rotation Monday). Positions opened at a rotation are held until
the next rotation Monday; only the daily exit rules (stop-loss / target /
thesis breaker) may close them mid-cycle, in which case the freed slot
stays in cash until the next rotation.

Cadence decision (C1 calibration, 2026-07-04): bi-weekly rotation beat
weekly on return, Sharpe, max drawdown and cost drag over 2018-2026.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import yaml

_THRESHOLDS_PATH = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"

DEFAULT_ROTATION_WEEKS = 1


def week_start_monday(day: date) -> date:
    """Return the Monday of the week containing ``day``."""
    return day - timedelta(days=day.weekday())


def load_rotation_config(config_path: Path | None = None) -> tuple[int, date | None]:
    """Read (rotation_weeks, rotation_anchor_monday) from thresholds.yaml.

    Missing file/keys fall back to weekly cadence with no anchor, which
    reproduces the legacy every-Monday behavior.
    """
    path = config_path or _THRESHOLDS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            selection = (yaml.safe_load(f) or {}).get("selection", {}) or {}
    except OSError:
        return DEFAULT_ROTATION_WEEKS, None

    try:
        weeks = max(1, int(selection.get("rotation_weeks", DEFAULT_ROTATION_WEEKS)))
    except (TypeError, ValueError):
        weeks = DEFAULT_ROTATION_WEEKS

    anchor_raw = selection.get("rotation_anchor_monday")
    anchor: date | None = None
    if isinstance(anchor_raw, date):
        anchor = anchor_raw
    elif anchor_raw:
        try:
            anchor = date.fromisoformat(str(anchor_raw).strip())
        except ValueError:
            anchor = None
    if anchor is not None:
        anchor = week_start_monday(anchor)
    return weeks, anchor


def rotation_cycle_start(
    today: date,
    rotation_weeks: int = DEFAULT_ROTATION_WEEKS,
    anchor_monday: date | None = None,
) -> date:
    """Return the rotation Monday of the cycle containing ``today``.

    With weekly cadence (or no anchor) this is simply the week's Monday.
    Python's floored modulo keeps the parity correct even for dates before
    the anchor.
    """
    monday = week_start_monday(today)
    rotation_weeks = max(1, int(rotation_weeks))
    if rotation_weeks == 1 or anchor_monday is None:
        return monday
    offset_weeks = ((monday - anchor_monday).days // 7) % rotation_weeks
    return monday - timedelta(weeks=offset_weeks)


def next_rotation_date(
    today: date,
    rotation_weeks: int = DEFAULT_ROTATION_WEEKS,
    anchor_monday: date | None = None,
) -> date:
    """Return the first rotation Monday strictly after ``today``."""
    cycle_start = rotation_cycle_start(today, rotation_weeks, anchor_monday)
    nxt = cycle_start + timedelta(weeks=max(1, int(rotation_weeks)))
    # cycle_start <= today by construction, so nxt is always in the future
    # except when today IS a rotation Monday — the next one is still ahead.
    return nxt
