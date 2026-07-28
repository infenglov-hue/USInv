"""Execution cost model charged inside every simulated fill."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

MONEY_QUANTUM = Decimal("0.01")


class CostModelError(ValueError):
    """Raised for an invalid cost configuration or trade notional."""


def money(value: Decimal) -> Decimal:
    """Round a USD ledger amount to cents deterministically."""
    if not value.is_finite():
        raise CostModelError("money value must be finite")
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class FixedBpsCostModel:
    """Symmetric one-way spread/impact charge."""

    basis_points: int = 40

    def __post_init__(self) -> None:
        if not 0 <= self.basis_points <= 10_000:
            raise CostModelError("cost basis points must be in [0, 10000]")

    def charge(self, notional: Decimal) -> Decimal:
        if not notional.is_finite() or notional < 0:
            raise CostModelError("trade notional must be finite and non-negative")
        return money(notional * Decimal(self.basis_points) / Decimal(10_000))
