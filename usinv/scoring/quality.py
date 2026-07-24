"""Quality / profitability sleeve (MODEL_SPEC §1).

Gross profitability (GP/Assets), ROIC, accruals (negative) and FCF conversion.
Accruals are inverted so that, like every other metric here, a higher value is
better: high accruals (earnings unsupported by cash) are a negative signal.
"""

from __future__ import annotations

from usinv.scoring.metrics import FundamentalInputs, safe_ratio

QUALITY_METRICS = ("gross_profitability", "roic", "negative_accruals", "fcf_conversion")


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
