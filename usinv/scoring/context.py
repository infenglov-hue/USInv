"""Point-in-time scoring context — the single ``as_of(date)`` API (MODEL_SPEC §7).

Both the backtest and the live path read data through one context bound to a
signal instant. A value is visible only when its availability instant is at or
before the signal (EDGAR ``acceptanceDateTime`` for fundamentals, official
publication time for macro, session close for prices; AGENTS.md rule 1). Reaching
for anything newer than the signal raises :class:`LookAheadError` — look-ahead
leakage is a hard error, never a silent read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime


class ScoringContextError(ValueError):
    """Base error for the scoring context."""


class LookAheadError(ScoringContextError):
    """Raised when data newer than the signal instant is accessed."""


@dataclass(frozen=True, slots=True)
class Vintage[T]:
    """A value tagged with the instant it first became available (as-first-filed)."""

    available_from: datetime
    value: T

    def __post_init__(self) -> None:
        if self.available_from.tzinfo is None:
            raise ScoringContextError("vintage available_from must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """All vintaged channels a signal can read, keyed by security or series id."""

    fundamentals: Mapping[str, Sequence[Vintage[object]]] = field(default_factory=dict)
    prices: Mapping[str, Sequence[Vintage[object]]] = field(default_factory=dict)
    macro: Mapping[str, Sequence[Vintage[object]]] = field(default_factory=dict)
    universe: Sequence[Vintage[str]] = ()

    def as_of(self, at: datetime) -> ScoringContext:
        """Bind these inputs to a signal instant *at* (must be timezone-aware)."""
        if at.tzinfo is None:
            raise ScoringContextError("as_of instant must be timezone-aware")
        return ScoringContext(at=at, inputs=self)


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """A read-only, point-in-time view of :class:`ScoringInputs` at ``at``."""

    at: datetime
    inputs: ScoringInputs

    # -- availability guards -------------------------------------------------- #
    def is_available(self, available_from: datetime) -> bool:
        """Whether a value with this availability instant is visible at the signal."""
        if available_from.tzinfo is None:
            raise ScoringContextError("available_from must be timezone-aware")
        return available_from <= self.at

    def require_available(self, available_from: datetime, description: str = "value") -> None:
        """Raise :class:`LookAheadError` if *available_from* is after the signal instant."""
        if not self.is_available(available_from):
            raise LookAheadError(
                f"{description} available_from {available_from.isoformat()} "
                f"is after as_of {self.at.isoformat()}"
            )

    # -- channel access ------------------------------------------------------- #
    def _latest[T](self, vintages: Iterable[Vintage[T]]) -> T | None:
        visible = [vintage for vintage in vintages if vintage.available_from <= self.at]
        if not visible:
            return None
        return max(visible, key=lambda vintage: vintage.available_from).value

    def fundamentals_ttm(self, security_id: str) -> object | None:
        """Latest as-first-filed TTM fundamentals visible at the signal, or None."""
        return self._latest(self.inputs.fundamentals.get(security_id, ()))

    def price(self, security_id: str) -> object | None:
        """Latest price bar whose session close is at or before the signal, or None."""
        return self._latest(self.inputs.prices.get(security_id, ()))

    def macro(self, series_id: str) -> object | None:
        """Latest macro observation published at or before the signal, or None."""
        return self._latest(self.inputs.macro.get(series_id, ()))

    def universe_members(self) -> tuple[str, ...]:
        """Security ids whose membership is confirmed at or before the signal."""
        members = {
            vintage.value for vintage in self.inputs.universe if vintage.available_from <= self.at
        }
        return tuple(sorted(members))

    def is_member(self, security_id: str) -> bool:
        """Whether *security_id* is a universe member at the signal instant."""
        return security_id in self.universe_members()
