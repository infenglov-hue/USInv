"""Portfolio rotation schedule bound to the XNYS calendar."""

from __future__ import annotations

from datetime import date

from usinv.calendar import Rotation, XNYSCalendar


def rotation_schedule(
    first_monday_anchor: date,
    through: date,
    *,
    weeks: int,
    calendar: XNYSCalendar | None = None,
) -> tuple[Rotation, ...]:
    """Return fixed-anchor rotations without rebasing after holidays/half-days."""
    engine = calendar or XNYSCalendar()
    return engine.rotations(first_monday_anchor, through, weeks=weeks)
