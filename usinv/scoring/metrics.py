"""Shared factor-metric inputs and safe ratio arithmetic (MODEL_SPEC §1).

A missing input is never imputed as zero (AGENTS.md rule: missing fundamentals are
never converted to zero without explicit structural evidence). Every ratio helper
returns ``None`` when it cannot be computed honestly, and callers must carry that
``None`` through ranking rather than substituting a neutral value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation


@dataclass(frozen=True, slots=True)
class FundamentalInputs:
    """As-first-filed TTM fundamentals for one security at one signal instant."""

    gross_profit: Decimal | None = None
    total_assets: Decimal | None = None
    ebit: Decimal | None = None
    total_enterprise_value: Decimal | None = None
    free_cash_flow: Decimal | None = None
    market_cap: Decimal | None = None
    net_income: Decimal | None = None
    cash_from_operations: Decimal | None = None
    invested_capital: Decimal | None = None


def safe_ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
    *,
    require_positive_denominator: bool = True,
) -> float | None:
    """Return ``numerator / denominator`` as a float, or ``None`` if undefined.

    A non-positive denominator makes scale-normalised ratios (yields, returns on
    capital) meaningless rather than merely negative, so by default it yields
    ``None`` instead of a misleading sign flip.
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    if require_positive_denominator and denominator < 0:
        return None
    try:
        return float(numerator / denominator)
    except (DivisionByZero, InvalidOperation, ArithmeticError):
        return None
