"""Point-in-time EOD stop and thesis-break exits (MODEL_SPEC §6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.hygiene.screen import HygieneScreenResult
from usinv.hygiene.verdict import HygieneAction
from usinv.portfolio.continuity import PositionState


class ExitError(ValueError):
    """Raised when an exit would use incomplete or look-ahead evidence."""


class ExitKind(StrEnum):
    """Exit causes that feed the common execution state machine."""

    PERCENT_STOP = "percent_stop"
    ATR_STOP = "atr_stop"
    THESIS_BREAK = "thesis_break"


@dataclass(frozen=True, slots=True)
class SplitContinuousBar:
    """One as-of split-continuous OHLC bar, anchored to its own session."""

    security_id: str
    session: date
    available_from: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjustment_quality: str
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.security_id or not self.evidence_pointer or not self.adjustment_quality:
            raise ExitError("bar identity, quality and evidence pointer are required")
        if self.available_from.tzinfo is None:
            raise ExitError("bar availability must be timezone-aware")
        prices = (self.open, self.high, self.low, self.close)
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise ExitError("OHLC values must be finite and positive")
        if self.high < max(self.open, self.low, self.close):
            raise ExitError("bar high is below another OHLC value")
        if self.low > min(self.open, self.high, self.close):
            raise ExitError("bar low is above another OHLC value")


@dataclass(frozen=True, slots=True)
class ExitSignal:
    """A close-time decision that must execute through the next-open state machine."""

    security_id: str
    kind: ExitKind
    signal_session: date
    execute_session: date
    collar_fraction: Decimal
    reference_close: Decimal
    threshold: Decimal | None
    reason: str
    evidence_pointers: tuple[str, ...]
    updated_high_water_mark: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StopEvaluation:
    """EOD state update plus an optional next-open exit signal."""

    updated_high_water_mark: Decimal
    atr14: Decimal | None
    signal: ExitSignal | None


HARD_THESIS_GATES = frozenset(
    {
        "atm_dilution",
        "going_concern",
        "listing_risk",
        "enforcement",
    }
)
ROUTINE_EXIT_COLLAR = Decimal("0.05")
THESIS_EXIT_COLLAR = Decimal("0.10")


def _visible_bar(
    bar: SplitContinuousBar,
    *,
    as_of: datetime,
    calendar: XNYSCalendar,
) -> None:
    if as_of.tzinfo is None:
        raise ExitError("exit as_of must be timezone-aware")
    try:
        session = calendar.session(bar.session)
    except CalendarError as exc:
        raise ExitError("stop bar is not an XNYS session") from exc
    close_utc = session.close_at.astimezone(UTC)
    available_utc = bar.available_from.astimezone(UTC)
    as_of_utc = as_of.astimezone(UTC)
    if available_utc < close_utc:
        raise ExitError("EOD bar cannot be available before the official close")
    if close_utc > as_of_utc or available_utc > as_of_utc:
        raise ExitError("stop evaluation attempted to use a future/unavailable bar")
    if bar.adjustment_quality == "unresolved":
        raise ExitError("stop calculation rejects unresolved split adjustments")


def _next_open_signal(
    *,
    security_id: str,
    kind: ExitKind,
    signal_session: date,
    collar_fraction: Decimal,
    reference_close: Decimal,
    threshold: Decimal | None,
    reason: str,
    evidence_pointers: tuple[str, ...],
    updated_high_water_mark: Decimal | None,
    calendar: XNYSCalendar,
) -> ExitSignal:
    return ExitSignal(
        security_id=security_id,
        kind=kind,
        signal_session=signal_session,
        execute_session=calendar.next_session(signal_session).label,
        collar_fraction=collar_fraction,
        reference_close=reference_close,
        threshold=threshold,
        reason=reason,
        evidence_pointers=tuple(sorted(set(evidence_pointers))),
        updated_high_water_mark=updated_high_water_mark,
    )


def evaluate_percent_trailing_stop(
    position: PositionState,
    bar: SplitContinuousBar,
    *,
    as_of: datetime,
    stop_fraction: Decimal,
    calendar: XNYSCalendar | None = None,
) -> StopEvaluation:
    """Evaluate a strict percent trailing stop at an official EOD close."""
    session_calendar = calendar or default_calendar()
    _visible_bar(bar, as_of=as_of, calendar=session_calendar)
    if bar.security_id != position.security_id:
        raise ExitError("position and stop bar security ids differ")
    if bar.session < position.entry_session:
        raise ExitError("stop bar predates the position entry")
    if not stop_fraction.is_finite() or not Decimal(0) < stop_fraction < Decimal(1):
        raise ExitError("percent stop fraction must be in (0, 1)")

    high_water = max(position.high_water_mark, bar.close)
    threshold = high_water * (Decimal(1) - stop_fraction)
    signal = None
    if bar.close < threshold:
        signal = _next_open_signal(
            security_id=position.security_id,
            kind=ExitKind.PERCENT_STOP,
            signal_session=bar.session,
            collar_fraction=ROUTINE_EXIT_COLLAR,
            reference_close=bar.close,
            threshold=threshold,
            reason=(
                f"official close {bar.close} below "
                f"{stop_fraction} trailing threshold {threshold}"
            ),
            evidence_pointers=(bar.evidence_pointer,),
            updated_high_water_mark=high_water,
            calendar=session_calendar,
        )
    return StopEvaluation(high_water, None, signal)


def wilder_atr(
    bars: tuple[SplitContinuousBar, ...],
    *,
    as_of: datetime,
    period: int = 14,
    calendar: XNYSCalendar | None = None,
) -> Decimal:
    """Return exact-decimal Wilder ATR using only bars visible at ``as_of``."""
    if period <= 0:
        raise ExitError("ATR period must be positive")
    if not bars:
        raise ExitError("ATR requires bars")
    session_calendar = calendar or default_calendar()
    ordered = tuple(sorted(bars, key=lambda item: item.session))
    if len({bar.session for bar in ordered}) != len(ordered):
        raise ExitError("ATR bars contain duplicate sessions")
    if len({bar.security_id for bar in ordered}) != 1:
        raise ExitError("ATR bars must belong to one security")
    for bar in ordered:
        _visible_bar(bar, as_of=as_of, calendar=session_calendar)
    if len(ordered) < period:
        raise ExitError(f"ATR({period}) requires at least {period} visible bars")

    true_ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for bar in ordered:
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = bar.close
    atr = sum(true_ranges[:period], start=Decimal(0)) / Decimal(period)
    for true_range in true_ranges[period:]:
        atr = ((atr * Decimal(period - 1)) + true_range) / Decimal(period)
    return atr


def evaluate_atr_trailing_stop(
    position: PositionState,
    bars: tuple[SplitContinuousBar, ...],
    *,
    as_of: datetime,
    multiple: Decimal = Decimal(3),
    period: int = 14,
    calendar: XNYSCalendar | None = None,
) -> StopEvaluation:
    """Evaluate close < max(close since entry) - multiple x Wilder ATR."""
    if not multiple.is_finite() or multiple <= 0:
        raise ExitError("ATR multiple must be finite and positive")
    session_calendar = calendar or default_calendar()
    ordered = tuple(sorted(bars, key=lambda item: item.session))
    if not ordered:
        raise ExitError("ATR stop requires bars")
    if ordered[-1].security_id != position.security_id:
        raise ExitError("position and ATR bars security ids differ")
    if any(bar.session < position.entry_session for bar in ordered):
        raise ExitError("ATR stop input contains a pre-entry bar")
    atr14 = wilder_atr(
        ordered,
        as_of=as_of,
        period=period,
        calendar=session_calendar,
    )
    current = ordered[-1]
    high_water = max(bar.close for bar in ordered)
    threshold = high_water - (multiple * atr14)
    signal = None
    if current.close < threshold:
        signal = _next_open_signal(
            security_id=position.security_id,
            kind=ExitKind.ATR_STOP,
            signal_session=current.session,
            collar_fraction=ROUTINE_EXIT_COLLAR,
            reference_close=current.close,
            threshold=threshold,
            reason=f"official close {current.close} below ATR threshold {threshold}",
            evidence_pointers=tuple(bar.evidence_pointer for bar in ordered),
            updated_high_water_mark=high_water,
            calendar=session_calendar,
        )
    return StopEvaluation(high_water, atr14, signal)


def evaluate_thesis_break(
    screen: HygieneScreenResult,
    *,
    signal_session: date,
    reference_close: Decimal,
    as_of: datetime,
    calendar: XNYSCalendar | None = None,
) -> ExitSignal | None:
    """Map a newly firing held-name hard gate to a 10% next-open exit."""
    session_calendar = calendar or default_calendar()
    if as_of.tzinfo is None:
        raise ExitError("thesis-break as_of must be timezone-aware")
    session = session_calendar.session(signal_session)
    if session.close_at.astimezone(UTC) > as_of.astimezone(UTC):
        raise ExitError("thesis-break signal session is newer than as_of")
    if not reference_close.is_finite() or reference_close <= 0:
        raise ExitError("thesis-break reference close must be finite and positive")
    firing = tuple(
        verdict
        for verdict in screen.firing
        if verdict.gate in HARD_THESIS_GATES and verdict.action is HygieneAction.EXCLUDE
    )
    if not firing:
        return None
    return _next_open_signal(
        security_id=screen.security_id,
        kind=ExitKind.THESIS_BREAK,
        signal_session=signal_session,
        collar_fraction=THESIS_EXIT_COLLAR,
        reference_close=reference_close,
        threshold=None,
        reason="hard hygiene gate fired: " + ", ".join(sorted(item.gate for item in firing)),
        evidence_pointers=tuple(item.evidence_pointer for item in firing),
        updated_high_water_mark=None,
        calendar=session_calendar,
    )
