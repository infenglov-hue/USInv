from __future__ import annotations

from datetime import UTC, date, time
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from usinv.calendar import (
    CalendarError,
    IneligibleSessionError,
    NotSessionError,
    XNYSCalendar,
)


@pytest.fixture(scope="module")
def xnys() -> XNYSCalendar:
    return XNYSCalendar(start="2019-01-01", end="2027-12-31")


def test_known_holiday_traps_and_juneteenth_cutover(xnys: XNYSCalendar) -> None:
    assert not xnys.is_session("2026-04-03")  # Good Friday
    assert not xnys.is_session("2026-07-03")  # observed Independence Day
    assert xnys.is_session("2021-06-18")  # before NYSE Juneteenth adoption
    assert not xnys.is_session("2022-06-20")  # first observed Juneteenth closure

    with pytest.raises(NotSessionError, match="closed"):
        xnys.session("2026-07-03")


def test_half_day_has_official_1300_et_close(xnys: XNYSCalendar) -> None:
    thanksgiving_friday = xnys.session("2025-11-28")

    assert thanksgiving_friday.is_half_day
    assert thanksgiving_friday.open_at.timetz().replace(tzinfo=None) == time(9, 30)
    assert thanksgiving_friday.close_at.timetz().replace(tzinfo=None) == time(13, 0)
    assert thanksgiving_friday.close_at.tzinfo == ZoneInfo("America/New_York")


def test_previous_next_and_t_minus_one_mapping_cross_closures(xnys: XNYSCalendar) -> None:
    assert xnys.previous_session("2026-07-06").label == date(2026, 7, 2)
    assert xnys.next_session("2026-07-02").label == date(2026, 7, 6)

    pair = xnys.signal_fill("2026-07-06", full_sessions_only=True)
    assert pair.signal.label == date(2026, 7, 2)
    assert pair.fill.label == date(2026, 7, 6)


def test_dst_boundaries_keep_et_clock_and_shift_utc_and_istanbul(xnys: XNYSCalendar) -> None:
    istanbul = ZoneInfo("Europe/Istanbul")
    before_spring = xnys.session("2026-03-06")
    after_spring = xnys.session("2026-03-09")
    before_fall = xnys.session("2026-10-30")
    after_fall = xnys.session("2026-11-02")

    for trading_session in (before_spring, after_spring, before_fall, after_fall):
        assert trading_session.open_at.timetz().replace(tzinfo=None) == time(9, 30)

    assert before_spring.open_at.astimezone(UTC).time() == time(14, 30)
    assert after_spring.open_at.astimezone(UTC).time() == time(13, 30)
    assert before_fall.open_at.astimezone(UTC).time() == time(13, 30)
    assert after_fall.open_at.astimezone(UTC).time() == time(14, 30)
    assert before_spring.open_at.astimezone(istanbul).time() == time(17, 30)
    assert after_spring.open_at.astimezone(istanbul).time() == time(16, 30)
    assert before_fall.open_at.astimezone(istanbul).time() == time(16, 30)
    assert after_fall.open_at.astimezone(istanbul).time() == time(17, 30)


def test_mlk_shift_does_not_rebase_next_anchor(xnys: XNYSCalendar) -> None:
    schedule = xnys.rotations("2026-01-19", "2026-02-16", weeks=4)

    assert [item.anchor for item in schedule] == [date(2026, 1, 19), date(2026, 2, 16)]
    assert schedule[0].signal.label == date(2026, 1, 16)
    assert schedule[0].fill.label == date(2026, 1, 20)
    assert schedule[0].shifted
    assert schedule[1].anchor == date(2026, 2, 16)


def test_thanksgiving_half_day_signal_shifts_monday_fill(xnys: XNYSCalendar) -> None:
    unshifted = xnys.signal_fill("2025-12-01")
    assert unshifted.signal.label == date(2025, 11, 28)
    assert unshifted.signal.is_half_day
    with pytest.raises(IneligibleSessionError, match="full XNYS sessions"):
        xnys.signal_fill("2025-12-01", full_sessions_only=True)

    shifted = xnys.rotations("2025-12-01", "2025-12-01", weeks=4)[0]
    assert shifted.anchor == date(2025, 12, 1)
    assert shifted.signal.label == date(2025, 12, 1)
    assert shifted.fill.label == date(2025, 12, 2)


@pytest.mark.parametrize("anchor", ["2026-05-25", "2026-09-07"])
def test_memorial_and_labor_day_mondays_shift_to_tuesday(xnys: XNYSCalendar, anchor: str) -> None:
    rotation = xnys.rotations(anchor, anchor, weeks=4)[0]
    assert rotation.fill.label.weekday() == 1
    assert rotation.shifted


def test_half_day_anchor_and_half_day_signal_are_both_skipped() -> None:
    xnys = XNYSCalendar(start="2023-01-01", end="2023-12-31")
    rotation = xnys.rotations("2023-07-03", "2023-07-03", weeks=4)[0]

    assert rotation.anchor == date(2023, 7, 3)
    assert rotation.signal.label == date(2023, 7, 5)
    assert rotation.fill.label == date(2023, 7, 6)


def test_rotation_contract_rejects_non_monday_and_unregistered_cadence(
    xnys: XNYSCalendar,
) -> None:
    with pytest.raises(CalendarError, match="Monday"):
        xnys.rotations("2026-01-20", "2026-02-20", weeks=4)
    with pytest.raises(CalendarError, match="one of"):
        xnys.rotations("2026-01-05", "2026-02-02", weeks=3)


@pytest.mark.parametrize("weeks", [2, 4, 6, 13])
def test_all_registered_rotation_series_remain_fixed_and_full(
    xnys: XNYSCalendar, weeks: int
) -> None:
    schedule = xnys.rotations("2020-01-06", "2027-12-27", weeks=weeks)

    for previous, current in pairwise(schedule):
        assert (current.anchor - previous.anchor).days == weeks * 7
    for rotation in schedule:
        assert rotation.fill.label >= rotation.anchor
        assert not rotation.signal.is_half_day
        assert not rotation.fill.is_half_day
        assert xnys.previous_session(rotation.fill.label).label == rotation.signal.label
