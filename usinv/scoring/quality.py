"""Quality / profitability sleeve (MODEL_SPEC §1).

Gross profitability (GP/Assets), ROIC, accruals (negative) and FCF conversion.
Accruals are inverted so that, like every other metric here, a higher value is
better: high accruals (earnings unsupported by cash) are a negative signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from usinv.scoring.metrics import FundamentalInputs, safe_ratio

QUALITY_METRICS = ("gross_profitability", "roic", "negative_accruals", "fcf_conversion")
PIOTROSKI_SIGNALS = (
    "positive_roa",
    "positive_cfo",
    "improving_roa",
    "cash_backed_earnings",
    "lower_leverage",
    "improving_current_ratio",
    "no_share_issuance",
    "improving_gross_margin",
    "improving_asset_turnover",
)


@dataclass(frozen=True, slots=True)
class PiotroskiInputs:
    """Current and prior PIT fundamentals needed by the nine-point F-score."""

    net_income: Decimal | None = None
    cash_from_operations: Decimal | None = None
    total_assets: Decimal | None = None
    long_term_debt: Decimal | None = None
    current_assets: Decimal | None = None
    current_liabilities: Decimal | None = None
    shares_outstanding: Decimal | None = None
    gross_profit: Decimal | None = None
    revenue: Decimal | None = None
    prior_net_income: Decimal | None = None
    prior_total_assets: Decimal | None = None
    prior_long_term_debt: Decimal | None = None
    prior_current_assets: Decimal | None = None
    prior_current_liabilities: Decimal | None = None
    prior_shares_outstanding: Decimal | None = None
    prior_gross_profit: Decimal | None = None
    prior_revenue: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PiotroskiResult:
    """Auditable F-score result; an incomplete score can never pass the veto."""

    signals: tuple[tuple[str, bool | None], ...]
    score: int
    complete: bool

    def vetoed(self, maximum_passing_junk_score: int) -> bool | None:
        """Return True for junk, False for a pass, or None when evidence is incomplete."""
        if not self.complete:
            return None
        return self.score <= maximum_passing_junk_score


def _finite(*values: Decimal | None) -> bool:
    return all(value is not None and value.is_finite() for value in values)


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if not _finite(numerator, denominator) or denominator == 0:
        return None
    try:
        return numerator / denominator
    except (InvalidOperation, ArithmeticError):
        return None


def _positive(value: Decimal | None) -> bool | None:
    return value > 0 if _finite(value) else None


def _greater(left: Decimal | None, right: Decimal | None) -> bool | None:
    return left > right if _finite(left, right) else None


def _not_greater(left: Decimal | None, right: Decimal | None) -> bool | None:
    return left <= right if _finite(left, right) else None


def piotroski_score(inputs: PiotroskiInputs) -> PiotroskiResult:
    """Compute the classic nine signals without treating missing facts as zero.

    Ratios use the current/prior period-end asset bases consistently. This is a
    junk veto, not a ranked factor: callers must quarantine an incomplete result
    and exclude a complete score at or below the configured threshold.
    """
    roa = _ratio(inputs.net_income, inputs.total_assets)
    prior_roa = _ratio(inputs.prior_net_income, inputs.prior_total_assets)
    leverage = _ratio(inputs.long_term_debt, inputs.total_assets)
    prior_leverage = _ratio(inputs.prior_long_term_debt, inputs.prior_total_assets)
    current_ratio = _ratio(inputs.current_assets, inputs.current_liabilities)
    prior_current_ratio = _ratio(
        inputs.prior_current_assets,
        inputs.prior_current_liabilities,
    )
    gross_margin = _ratio(inputs.gross_profit, inputs.revenue)
    prior_gross_margin = _ratio(inputs.prior_gross_profit, inputs.prior_revenue)
    asset_turnover = _ratio(inputs.revenue, inputs.total_assets)
    prior_asset_turnover = _ratio(inputs.prior_revenue, inputs.prior_total_assets)

    values: tuple[bool | None, ...] = (
        _positive(roa),
        _positive(inputs.cash_from_operations),
        _greater(roa, prior_roa),
        _greater(inputs.cash_from_operations, inputs.net_income),
        _greater(prior_leverage, leverage),
        _greater(current_ratio, prior_current_ratio),
        _not_greater(inputs.shares_outstanding, inputs.prior_shares_outstanding),
        _greater(gross_margin, prior_gross_margin),
        _greater(asset_turnover, prior_asset_turnover),
    )
    signals = tuple(zip(PIOTROSKI_SIGNALS, values, strict=True))
    return PiotroskiResult(
        signals=signals,
        score=sum(value is True for value in values),
        complete=all(value is not None for value in values),
    )


def value_of_negative_accruals(inputs: FundamentalInputs) -> float | None:
    """Return -(NI - CFO)/assets: higher means earnings are better cash-backed."""
    if inputs.net_income is None or inputs.cash_from_operations is None:
        return None
    accrual = safe_ratio(inputs.net_income - inputs.cash_from_operations, inputs.total_assets)
    return None if accrual is None else -accrual


def quality_metrics(inputs: FundamentalInputs) -> dict[str, float | None]:
    """Return the quality sleeve's raw metrics; unavailable ones stay ``None``."""
    return {
        "gross_profitability": safe_ratio(inputs.gross_profit, inputs.total_assets),
        "roic": safe_ratio(inputs.ebit, inputs.invested_capital),
        "negative_accruals": value_of_negative_accruals(inputs),
        # v1 FCF conversion is cash generation per unit of operating profit; EBIT
        # must be positive for the ratio to mean "conversion" at all.
        "fcf_conversion": safe_ratio(inputs.free_cash_flow, inputs.ebit),
    }
