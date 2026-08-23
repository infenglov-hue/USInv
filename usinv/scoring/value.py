"""Value sleeve (MODEL_SPEC §1).

A multi-ratio composite — EBIT/TEV, FCF yield, earnings yield — not P/B alone,
because multi-ratio composites degrade less than single ratios post-publication.
Every metric is oriented so that a higher value is cheaper (better).
"""

from __future__ import annotations

from usinv.scoring.metrics import FundamentalInputs, safe_ratio

VALUE_METRICS = ("ebit_to_tev", "fcf_yield", "earnings_yield")


def value_metrics(inputs: FundamentalInputs) -> dict[str, float | None]:
    """Return the value sleeve's raw metrics; unavailable ones stay ``None``."""
    return {
        "ebit_to_tev": safe_ratio(inputs.ebit, inputs.total_enterprise_value),
        "fcf_yield": safe_ratio(inputs.free_cash_flow, inputs.market_cap),
        "earnings_yield": safe_ratio(inputs.net_income, inputs.market_cap),
    }
