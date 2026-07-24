"""Phase 3.3 factor sleeve + cross-sectional ranking tests (MODEL_SPEC §1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from usinv.scoring import (
    FundamentalInputs,
    MomentumInputs,
    momentum_12_1,
    momentum_metrics,
    percentile_ranks,
    quality_metrics,
    rank_sleeve,
    safe_ratio,
    sleeve_score,
    value_metrics,
)

D = Decimal


# --------------------------------------------------------------------------- #
# safe_ratio never imputes.
# --------------------------------------------------------------------------- #
def test_safe_ratio_basic():
    assert safe_ratio(D("50"), D("200")) == pytest.approx(0.25)


@pytest.mark.parametrize(
    "numerator,denominator",
    [(None, D("10")), (D("10"), None), (D("10"), D("0")), (D("10"), D("-5"))],
)
def test_safe_ratio_returns_none_instead_of_guessing(numerator, denominator):
    assert safe_ratio(numerator, denominator) is None


def test_safe_ratio_allows_negative_denominator_when_requested():
    assert safe_ratio(D("10"), D("-5"), require_positive_denominator=False) == pytest.approx(-2.0)


# --------------------------------------------------------------------------- #
# Value sleeve.
# --------------------------------------------------------------------------- #
def test_value_metrics_computed():
    metrics = value_metrics(
        FundamentalInputs(
            ebit=D("100"),
            total_enterprise_value=D("1000"),
            free_cash_flow=D("60"),
            market_cap=D("1200"),
            net_income=D("48"),
        )
    )
    assert metrics["ebit_to_tev"] == pytest.approx(0.1)
    assert metrics["fcf_yield"] == pytest.approx(0.05)
    assert metrics["earnings_yield"] == pytest.approx(0.04)


def test_value_metrics_missing_inputs_stay_none():
    metrics = value_metrics(FundamentalInputs(ebit=D("100")))
    assert metrics["ebit_to_tev"] is None
    assert metrics["fcf_yield"] is None


def test_negative_enterprise_value_is_undefined_not_negative():
    metrics = value_metrics(FundamentalInputs(ebit=D("100"), total_enterprise_value=D("-500")))
    assert metrics["ebit_to_tev"] is None


# --------------------------------------------------------------------------- #
# Quality sleeve.
# --------------------------------------------------------------------------- #
def test_quality_metrics_computed():
    metrics = quality_metrics(
        FundamentalInputs(
            gross_profit=D("300"),
            total_assets=D("1000"),
            ebit=D("100"),
            invested_capital=D("500"),
            net_income=D("80"),
            cash_from_operations=D("120"),
            free_cash_flow=D("90"),
        )
    )
    assert metrics["gross_profitability"] == pytest.approx(0.3)
    assert metrics["roic"] == pytest.approx(0.2)
    # NI 80 - CFO 120 = -40 accrual over 1000 assets ⇒ -(-0.04) = +0.04 (cash-backed).
    assert metrics["negative_accruals"] == pytest.approx(0.04)
    assert metrics["fcf_conversion"] == pytest.approx(0.9)


def test_accruals_sign_penalises_uncashed_earnings():
    cash_backed = quality_metrics(
        FundamentalInputs(total_assets=D("1000"), net_income=D("50"), cash_from_operations=D("90"))
    )["negative_accruals"]
    paper_earnings = quality_metrics(
        FundamentalInputs(total_assets=D("1000"), net_income=D("90"), cash_from_operations=D("50"))
    )["negative_accruals"]
    assert cash_backed > paper_earnings


# --------------------------------------------------------------------------- #
# Momentum sleeve.
# --------------------------------------------------------------------------- #
def test_momentum_12_1_uses_total_return_window():
    assert momentum_12_1(MomentumInputs(D("100"), D("130"))) == pytest.approx(0.30)


def test_momentum_missing_or_invalid_endpoint_is_none():
    assert momentum_12_1(MomentumInputs(None, D("130"))) is None
    assert momentum_12_1(MomentumInputs(D("0"), D("130"))) is None
    assert momentum_metrics(MomentumInputs(D("100"), D("90")))["momentum_12_1"] == pytest.approx(
        -0.10
    )


# --------------------------------------------------------------------------- #
# Cross-sectional ranking.
# --------------------------------------------------------------------------- #
def test_percentile_ranks_order_and_bounds():
    ranks = percentile_ranks({"a": 1.0, "b": 2.0, "c": 3.0})
    assert ranks["a"] < ranks["b"] < ranks["c"]
    assert ranks["a"] > 0.0
    assert ranks["c"] < 1.0


def test_percentile_ranks_ties_share_average():
    ranks = percentile_ranks({"a": 5.0, "b": 5.0, "c": 9.0})
    assert ranks["a"] == ranks["b"]
    assert ranks["c"] > ranks["a"]


def test_percentile_ranks_missing_stays_none_and_is_excluded():
    ranks = percentile_ranks({"a": 1.0, "b": None, "c": 3.0})
    assert ranks["b"] is None
    # b must not compress a and c toward a fabricated middle.
    assert ranks["a"] == pytest.approx(0.25)
    assert ranks["c"] == pytest.approx(0.75)


def test_percentile_ranks_all_missing():
    assert percentile_ranks({"a": None, "b": None}) == {"a": None, "b": None}


def test_sleeve_score_ignores_missing_metrics():
    assert sleeve_score([0.2, None, 0.8]) == pytest.approx(0.5)
    assert sleeve_score([None, None]) is None


def test_rank_sleeve_end_to_end():
    scores = rank_sleeve(
        {
            "CHEAP": {"ebit_to_tev": 0.20, "fcf_yield": 0.10},
            "MID": {"ebit_to_tev": 0.10, "fcf_yield": 0.05},
            "RICH": {"ebit_to_tev": 0.02, "fcf_yield": 0.01},
        }
    )
    assert scores["CHEAP"] > scores["MID"] > scores["RICH"]


def test_rank_sleeve_security_with_no_metrics_is_unscoreable():
    scores = rank_sleeve(
        {
            "GOOD": {"ebit_to_tev": 0.20, "fcf_yield": 0.10},
            "EMPTY": {"ebit_to_tev": None, "fcf_yield": None},
        }
    )
    assert scores["EMPTY"] is None
    assert scores["GOOD"] is not None
