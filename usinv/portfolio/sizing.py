"""Settled-cash-only equal-weight entry sizing."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class SizingError(ValueError):
    """Raised when NAV or settled-cash inputs are inconsistent."""


@dataclass(frozen=True, slots=True)
class EntryAllocation:
    security_id: str
    target_notional: Decimal
    funded_notional: Decimal
    funded: bool


@dataclass(frozen=True, slots=True)
class SizingPlan:
    target_per_position: Decimal
    allocations: tuple[EntryAllocation, ...]
    settled_cash_remaining: Decimal


def size_equal_weight_entries(
    ordered_security_ids: tuple[str, ...],
    *,
    pre_trade_nav: Decimal,
    settled_cash: Decimal,
    holdings_target: int,
) -> SizingPlan:
    """Fund full 1/N slots in rank order; partially funded slots remain cash."""
    if (
        not pre_trade_nav.is_finite()
        or not settled_cash.is_finite()
        or pre_trade_nav <= 0
        or settled_cash < 0
        or holdings_target <= 0
    ):
        raise SizingError("NAV, settled cash and holdings target are invalid")
    target = pre_trade_nav / Decimal(holdings_target)
    remaining = settled_cash
    allocations: list[EntryAllocation] = []
    for security_id in ordered_security_ids:
        if not security_id:
            raise SizingError("security ids cannot be blank")
        funded = remaining >= target
        funded_notional = target if funded else Decimal(0)
        if funded:
            remaining -= target
        allocations.append(
            EntryAllocation(
                security_id=security_id,
                target_notional=target,
                funded_notional=funded_notional,
                funded=funded,
            )
        )
    return SizingPlan(
        target_per_position=target,
        allocations=tuple(allocations),
        settled_cash_remaining=remaining,
    )
