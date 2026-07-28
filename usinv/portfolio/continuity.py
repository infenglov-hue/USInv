"""Position identity, cost basis and high-water continuity across rotations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal


class ContinuityError(ValueError):
    """Raised when a position transformation would violate accounting identity."""


@dataclass(frozen=True, slots=True)
class PositionState:
    position_id: str
    security_id: str
    ticker: str
    quantity: Decimal
    cost_basis_per_share: Decimal
    high_water_mark: Decimal
    entry_session: date

    def __post_init__(self) -> None:
        if not self.position_id or not self.security_id or not self.ticker:
            raise ContinuityError("position identity fields must be non-empty")
        values = (self.quantity, self.cost_basis_per_share, self.high_water_mark)
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ContinuityError("position quantity and price bases must be finite and positive")


def retain_position(position: PositionState, *, current_ticker: str | None = None) -> PositionState:
    """Carry a holding through rotation without resetting entry economics."""
    if current_ticker is None or current_ticker == position.ticker:
        return position
    if not current_ticker:
        raise ContinuityError("current ticker cannot be blank")
    return replace(position, ticker=current_ticker)


def apply_effective_split(position: PositionState, *, new_for_old: Decimal) -> PositionState:
    """Transform quantity, cost basis and high-water mark on an effective split."""
    if not new_for_old.is_finite() or new_for_old <= 0:
        raise ContinuityError("split ratio must be finite and positive")
    return replace(
        position,
        quantity=position.quantity * new_for_old,
        cost_basis_per_share=position.cost_basis_per_share / new_for_old,
        high_water_mark=position.high_water_mark / new_for_old,
    )
