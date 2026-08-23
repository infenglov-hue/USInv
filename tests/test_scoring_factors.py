"""Phase 3.3 factor sleeve + cross-sectional ranking tests (MODEL_SPEC §1)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from usinv.config.loader import FactorWeights
from usinv.scoring import (
    FactorCandidate,
    FundamentalInputs,
    MomentumInputs,
    PiotroskiInputs,
    momentum_12_1,
    momentum_metrics,
    percentile_ranks,
    piotroski_score,
    quality_metrics,
    rank_sleeve,
    safe_ratio,
    score_composite,
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


# --------------------------------------------------------------------------- #
# Piotroski junk veto.
# --------------------------------------------------------------------------- #
def _strong_piotroski() -> PiotroskiInputs:
    return PiotroskiInputs(
        net_income=D("12"),
        cash_from_operations=D("18"),
        total_assets=D("100"),
        long_term_debt=D("15"),
        current_assets=D("60"),
        current_liabilities=D("30"),
        shares_outstanding=D("10"),
        gross_profit=D("45"),
        revenue=D("90"),
        prior_net_income=D("5"),
        prior_total_assets=D("100"),
        prior_long_term_debt=D("25"),
        prior_current_assets=D("40"),
        prior_current_liabilities=D("30"),
        prior_shares_outstanding=D("10"),
        prior_gross_profit=D("30"),
        prior_revenue=D("80"),
    )


def test_piotroski_all_nine_signals_and_veto_boundary():
    result = piotroski_score(_strong_piotroski())
    assert result.complete is True
    assert result.score == 9
    assert result.vetoed(4) is False
    assert all(value is True for _, value in result.signals)


def test_piotroski_low_complete_score_is_junk_veto():
    weak = PiotroskiInputs(
        net_income=D("-10"),
        cash_from_operations=D("-12"),
        total_assets=D("120"),
        long_term_debt=D("50"),
        current_assets=D("20"),
        current_liabilities=D("40"),
        shares_outstanding=D("15"),
        gross_profit=D("15"),
        revenue=D("80"),
        prior_net_income=D("5"),
        prior_total_assets=D("100"),
        prior_long_term_debt=D("20"),
        prior_current_assets=D("40"),
        prior_current_liabilities=D("30"),
        prior_shares_outstanding=D("10"),
        prior_gross_profit=D("30"),
        prior_revenue=D("80"),
    )
    result = piotroski_score(weak)
    assert result.complete is True
    assert result.score <= 4
    assert result.vetoed(4) is True


def test_piotroski_missing_fact_is_unknown_not_a_synthetic_zero():
    incomplete = replace(_strong_piotroski(), prior_revenue=None)
    result = piotroski_score(incomplete)
    assert result.complete is False
    assert result.vetoed(4) is None
    assert dict(result.signals)["improving_gross_margin"] is None


# --------------------------------------------------------------------------- #
# Composite, peer groups and sector-relative ranks.
# --------------------------------------------------------------------------- #
WEIGHTS = FactorWeights(quality=0.4, value=0.3, momentum=0.3)


def _candidate(
    security_id: str,
    metric: float | None,
    *,
    bucket: str = "core",
    sector: str | None = "manufacturing",
    piotroski: PiotroskiInputs | None = None,
) -> FactorCandidate:
    return FactorCandidate(
        security_id=security_id,
        size_bucket=bucket,
        ff12_group=sector,
        value_metrics={"value": metric},
        quality_metrics={"quality": metric},
        momentum_metrics={"momentum": metric},
        piotroski_inputs=piotroski or _strong_piotroski(),
    )


def test_composite_applies_weights_and_returns_bucket_percentile():
    scores = score_composite(
        [_candidate("LOW", 1.0), _candidate("HIGH", 3.0)],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=False,
    )
    assert scores["HIGH"].eligible is True
    assert scores["HIGH"].composite == pytest.approx(0.75)
    assert scores["HIGH"].bucket_percentile == pytest.approx(0.75)
    assert scores["LOW"].composite == pytest.approx(0.25)


def test_piotroski_veto_happens_before_peer_percentiles():
    weak = PiotroskiInputs(
        net_income=D("-1"),
        cash_from_operations=D("-2"),
        total_assets=D("100"),
        long_term_debt=D("50"),
        current_assets=D("10"),
        current_liabilities=D("20"),
        shares_outstanding=D("20"),
        gross_profit=D("10"),
        revenue=D("100"),
        prior_net_income=D("1"),
        prior_total_assets=D("100"),
        prior_long_term_debt=D("10"),
        prior_current_assets=D("20"),
        prior_current_liabilities=D("20"),
        prior_shares_outstanding=D("10"),
        prior_gross_profit=D("20"),
        prior_revenue=D("100"),
    )
    scores = score_composite(
        [
            _candidate("LOW", 1.0),
            _candidate("HIGH", 2.0),
            _candidate("JUNK", 999.0, piotroski=weak),
        ],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=False,
    )
    assert scores["JUNK"].eligible is False
    assert scores["JUNK"].exclusion_reason == "piotroski_veto"
    # JUNK is absent from the fitted population: the two valid names are .25/.75.
    assert scores["HIGH"].composite == pytest.approx(0.75)


def test_incomplete_piotroski_fails_closed():
    scores = score_composite(
        [_candidate("UNKNOWN", 1.0, piotroski=PiotroskiInputs())],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=False,
    )
    assert scores["UNKNOWN"].eligible is False
    assert scores["UNKNOWN"].exclusion_reason == "piotroski_incomplete"
    assert scores["UNKNOWN"].composite is None


def test_size_buckets_are_always_ranked_independently():
    scores = score_composite(
        [
            _candidate("CORE_LOW", 1.0),
            _candidate("CORE_HIGH", 2.0),
            _candidate("LARGE_LOW", 100.0, bucket="large"),
            _candidate("LARGE_HIGH", 200.0, bucket="large"),
        ],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=False,
    )
    assert scores["CORE_LOW"].composite == pytest.approx(scores["LARGE_LOW"].composite)
    assert scores["CORE_HIGH"].bucket_percentile == pytest.approx(
        scores["LARGE_HIGH"].bucket_percentile
    )


def test_sector_relative_option_ranks_inside_ff12_within_bucket():
    candidates = [
        _candidate("TECH_HIGH", 10.0, sector="tech"),
        _candidate("TECH_LOW", 5.0, sector="tech"),
        _candidate("ENERGY_HIGH", 100.0, sector="energy"),
        _candidate("ENERGY_LOW", 1.0, sector="energy"),
    ]
    global_scores = score_composite(
        candidates,
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=False,
    )
    sector_scores = score_composite(
        candidates,
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=True,
    )
    assert global_scores["TECH_HIGH"].composite == pytest.approx(0.625)
    assert sector_scores["TECH_HIGH"].composite == pytest.approx(0.75)
    assert sector_scores["TECH_HIGH"].peer_group == "core/tech"


def test_sector_relative_missing_ff12_and_missing_sleeve_fail_closed():
    scores = score_composite(
        [
            _candidate("NO_SECTOR", 1.0, sector=None),
            _candidate("NO_MOMENTUM", 2.0),
        ],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=True,
    )
    assert scores["NO_SECTOR"].exclusion_reason == "missing_ff12_group"

    missing = FactorCandidate(
        security_id="EMPTY_MOMENTUM",
        size_bucket="core",
        ff12_group="tech",
        value_metrics={"value": 1.0},
        quality_metrics={"quality": 1.0},
        momentum_metrics={"momentum": None},
        piotroski_inputs=_strong_piotroski(),
    )
    score = score_composite(
        [missing],
        weights=WEIGHTS,
        piotroski_veto_max=4,
        sector_relative=True,
    )["EMPTY_MOMENTUM"]
    assert score.exclusion_reason == "missing_factor_sleeve"
    assert score.composite is None
