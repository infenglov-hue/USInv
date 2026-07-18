"""Deterministic XNYS session and fixed-anchor rotation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from exchange_calendars.exchange_calendar import ExchangeCalendar

EXCHANGE_TIMEZONE: Final = ZoneInfo("America/New_York")
DEFAULT_START: Final = date(1990, 1, 1)
DEFAULT_END: Final = date(2035, 12, 31)
REGULAR_CLOSE: Final = time(16, 0)
REGISTERED_ROTATION_WEEKS: Final = frozenset({2, 4, 6, 13})

SessionDate = date | str


class CalendarError(ValueError):
    """Base error for violations of the USInv exchange-time contract."""


class CalendarRangeError(CalendarError):
    """Raised when a request falls outside the calendar's declared range."""


class NotSessionError(CalendarError):
    """Raised when an exact session was required but the market was closed."""


class IneligibleSessionError(CalendarError):
    """Raised when a full-session-only mapping receives a half-day."""


@dataclass(frozen=True, slots=True)
class TradingSession:
    """One XNYS session with official timezone-aware open and close instants."""

    label: date
    open_at: datetime
    close_at: datetime
    is_half_day: bool


@dataclass(frozen=True, slots=True)
class SignalFill:
    """The exact T-1 signal session and T opening-auction fill session."""

    signal: TradingSession
    fill: TradingSession


@dataclass(frozen=True, slots=True)
class Rotation:
    """A fixed anchor and its first eligible full-session signal/fill pair."""

    anchor: date
    signal: TradingSession
    fill: TradingSession

    @property
    def shifted(self) -> bool:
        return self.fill.label != self.anchor


def _coerce_date(value: SessionDate) -> date:
    if isinstance(value, datetime):
        raise TypeError("session labels must be dates, not datetimes")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CalendarError(f"invalid ISO session date: {value!r}") from exc
    raise TypeError(f"session label must be date or ISO string, received {type(value).__name__}")


class XNYSCalendar:
    """Small stdlib-facing wrapper around a version-pinned XNYS calendar."""

    def __init__(self, start: SessionDate = DEFAULT_START, end: SessionDate = DEFAULT_END) -> None:
        self.start = _coerce_date(start)
        self.end = _coerce_date(end)
        if self.start >= self.end:
            raise CalendarRangeError("calendar start must be earlier than end")
        self._calendar: ExchangeCalendar = xcals.get_calendar(
            "XNYS", start=self.start, end=self.end
        )

    def _in_range(self, day: date) -> None:
        if not self.start <= day <= self.end:
            raise CalendarRangeError(
                f"{day.isoformat()} is outside {self.start.isoformat()}..{self.end.isoformat()}"
            )

    @staticmethod
    def _label(value: object) -> date:
        label = value.date()  # type: ignore[attr-defined]
        if not isinstance(label, date):
            raise TypeError("calendar returned a non-date session label")
        return label

    @staticmethod
    def _instant(value: object) -> datetime:
        instant = value.to_pydatetime()  # type: ignore[attr-defined]
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise TypeError("calendar returned a timezone-naive instant")
        return instant.astimezone(EXCHANGE_TIMEZONE)

    def is_session(self, value: SessionDate) -> bool:
        """Return whether *value* is an exact XNYS trading-session label."""
        day = _coerce_date(value)
        self._in_range(day)
        return bool(self._calendar.is_session(day))

    def session(self, value: SessionDate) -> TradingSession:
        """Return an exact session; holidays and weekends fail loudly."""
        day = _coerce_date(value)
        if not self.is_session(day):
            raise NotSessionError(f"XNYS is closed on {day.isoformat()}")
        open_at = self._instant(self._calendar.session_open(day))
        close_at = self._instant(self._calendar.session_close(day))
        return TradingSession(
            label=day,
            open_at=open_at,
            close_at=close_at,
            is_half_day=close_at.timetz().replace(tzinfo=None) < REGULAR_CLOSE,
        )

    def previous_session(self, value: SessionDate) -> TradingSession:
        """Return the closest session strictly before *value*."""
        day = _coerce_date(value)
        self._in_range(day)
        if self.is_session(day):
            label = self._calendar.previous_session(day)
        else:
            label = self._calendar.date_to_session(day, direction="previous")
        return self.session(self._label(label))

    def next_session(self, value: SessionDate) -> TradingSession:
        """Return the closest session strictly after *value*."""
        day = _coerce_date(value)
        self._in_range(day)
        if self.is_session(day):
            label = self._calendar.next_session(day)
        else:
            label = self._calendar.date_to_session(day, direction="next")
        return self.session(self._label(label))

    def signal_fill(self, fill: SessionDate, *, full_sessions_only: bool = False) -> SignalFill:
        """Map an exact T fill session to its previous-session T-1 signal."""
        fill_session = self.session(fill)
        signal_session = self.previous_session(fill_session.label)
        if full_sessions_only and (signal_session.is_half_day or fill_session.is_half_day):
            raise IneligibleSessionError("signal and fill must both be full XNYS sessions")
        return SignalFill(signal=signal_session, fill=fill_session)

    def eligible_signal_fill(self, value: SessionDate) -> SignalFill:
        """Find the first full T-1/T pair whose fill is on or after *value*."""
        candidate_day = _coerce_date(value)
        self._in_range(candidate_day)
        if self.is_session(candidate_day):
            candidate = self.session(candidate_day)
        else:
            label = self._calendar.date_to_session(candidate_day, direction="next")
            candidate = self.session(self._label(label))

        while True:
            pair = self.signal_fill(candidate.label)
            if not pair.signal.is_half_day and not pair.fill.is_half_day:
                return pair
            candidate = self.next_session(candidate.label)

    def rotations(
        self,
        first_anchor: SessionDate,
        through: SessionDate,
        *,
        weeks: int,
    ) -> tuple[Rotation, ...]:
        """Generate fixed Monday anchors and shift each independently when needed."""
        first = _coerce_date(first_anchor)
        final = _coerce_date(through)
        self._in_range(first)
        self._in_range(final)
        if first.weekday() != 0:
            raise CalendarError("first rotation anchor must be a Monday")
        if final < first:
            raise CalendarError("rotation end cannot precede the first anchor")
        if weeks not in REGISTERED_ROTATION_WEEKS:
            raise CalendarError(
                f"rotation weeks must be one of {sorted(REGISTERED_ROTATION_WEEKS)}"
            )

        step = timedelta(weeks=weeks)
        count = ((final - first) // step) + 1
        rotations: list[Rotation] = []
        for index in range(count):
            anchor = first + index * step
            pair = self.eligible_signal_fill(anchor)
            rotations.append(Rotation(anchor=anchor, signal=pair.signal, fill=pair.fill))
        return tuple(rotations)


@lru_cache(maxsize=1)
def default_calendar() -> XNYSCalendar:
    """Return the process-wide calendar with a deterministic support range."""
    return XNYSCalendar()


def is_session(value: SessionDate) -> bool:
    return default_calendar().is_session(value)


def session(value: SessionDate) -> TradingSession:
    return default_calendar().session(value)


def previous_session(value: SessionDate) -> TradingSession:
    return default_calendar().previous_session(value)


def next_session(value: SessionDate) -> TradingSession:
    return default_calendar().next_session(value)


def signal_fill(value: SessionDate, *, full_sessions_only: bool = False) -> SignalFill:
    return default_calendar().signal_fill(value, full_sessions_only=full_sessions_only)


def rotations(
    first_anchor: SessionDate, through: SessionDate, *, weeks: int
) -> tuple[Rotation, ...]:
    return default_calendar().rotations(first_anchor, through, weeks=weeks)
