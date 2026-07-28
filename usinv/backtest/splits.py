"""Locked static TRAIN/VALIDATION/TEST partitions with boundary purging."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from usinv.calendar import XNYSCalendar, default_calendar

TRAIN_START = date(2012, 1, 3)
TRAIN_END = date(2018, 12, 31)
VALIDATION_START = date(2019, 1, 2)
VALIDATION_END = date(2022, 12, 30)
TEST_START = date(2023, 1, 3)
TEST_END = date(2026, 6, 30)
LONGEST_GRID_ROTATION_SESSIONS = 65


class SplitProtocolError(ValueError):
    """Raised when a split would weaken the pre-registered holdout protocol."""


@dataclass(frozen=True, slots=True)
class PurgedBoundary:
    left_split: str
    right_split: str
    last_usable_session: date
    first_right_session: date
    purged_sessions: tuple[date, ...]
    maximum_holding_horizon_sessions: int


@dataclass(frozen=True, slots=True)
class LockedSplits:
    """Static windows; TEST retains its exact pre-registered endpoints."""

    train: tuple[date, ...]
    validation: tuple[date, ...]
    test: tuple[date, ...]
    raw_train: tuple[date, ...]
    raw_validation: tuple[date, ...]
    boundaries: tuple[PurgedBoundary, ...]
    maximum_holding_horizon_sessions: int

    def sessions(self, split: str) -> tuple[date, ...]:
        if split == "train":
            return self.train
        if split == "validation":
            return self.validation
        if split == "test":
            return self.test
        raise SplitProtocolError(f"unknown split: {split}")


@dataclass(frozen=True, slots=True)
class TrainingDiagnostic:
    """One rolling-origin diagnostic confined strictly to raw TRAIN."""

    fit_sessions: tuple[date, ...]
    evaluation_sessions: tuple[date, ...]
    purged_sessions: tuple[date, ...]
    may_select_final_config: bool = False


def _session_range(
    start: date,
    end: date,
    *,
    calendar: XNYSCalendar,
) -> tuple[date, ...]:
    first = calendar.session(start).label
    final = calendar.session(end).label
    sessions = [first]
    while sessions[-1] < final:
        sessions.append(calendar.next_session(sessions[-1]).label)
    if sessions[-1] != final:
        raise SplitProtocolError("locked endpoint is not reachable on the XNYS calendar")
    return tuple(sessions)


def build_locked_splits(
    *,
    maximum_holding_horizon_sessions: int,
    calendar: XNYSCalendar | None = None,
) -> LockedSplits:
    """Build static windows and purge the full registered horizon at each boundary."""
    if maximum_holding_horizon_sessions <= LONGEST_GRID_ROTATION_SESSIONS:
        raise SplitProtocolError(
            "maximum holding horizon must exceed the longest single grid rotation"
        )
    session_calendar = calendar or default_calendar()
    raw_train = _session_range(TRAIN_START, TRAIN_END, calendar=session_calendar)
    raw_validation = _session_range(
        VALIDATION_START,
        VALIDATION_END,
        calendar=session_calendar,
    )
    test = _session_range(TEST_START, TEST_END, calendar=session_calendar)
    horizon = maximum_holding_horizon_sessions
    if len(raw_train) <= horizon or len(raw_validation) <= horizon:
        raise SplitProtocolError("holding horizon consumes an entire pre-TEST split")

    train = raw_train[:-horizon]
    validation = raw_validation[:-horizon]
    boundaries = (
        PurgedBoundary(
            "train",
            "validation",
            train[-1],
            raw_validation[0],
            raw_train[-horizon:],
            horizon,
        ),
        PurgedBoundary(
            "validation",
            "test",
            validation[-1],
            test[0],
            raw_validation[-horizon:],
            horizon,
        ),
    )
    return LockedSplits(
        train,
        validation,
        test,
        raw_train,
        raw_validation,
        boundaries,
        horizon,
    )


def rolling_train_diagnostics(
    splits: LockedSplits,
    *,
    minimum_fit_sessions: int,
    evaluation_sessions: int,
    step_sessions: int,
) -> tuple[TrainingDiagnostic, ...]:
    """Create expanding-window diagnostics that never leave raw TRAIN."""
    if min(minimum_fit_sessions, evaluation_sessions, step_sessions) <= 0:
        raise SplitProtocolError("rolling diagnostic lengths must be positive")
    horizon = splits.maximum_holding_horizon_sessions
    raw = splits.raw_train
    first_evaluation = minimum_fit_sessions + horizon
    diagnostics: list[TrainingDiagnostic] = []
    for start in range(first_evaluation, len(raw) - evaluation_sessions + 1, step_sessions):
        fit = raw[: start - horizon]
        purged = raw[start - horizon : start]
        evaluation = raw[start : start + evaluation_sessions]
        if not fit or not evaluation or evaluation[-1] > TRAIN_END:
            raise SplitProtocolError("rolling diagnostic escaped TRAIN")
        diagnostics.append(TrainingDiagnostic(fit, evaluation, purged))
    return tuple(diagnostics)
