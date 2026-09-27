"""Unit tests for the AI Analyst investment thesis generator."""

from __future__ import annotations

from usinv.analysis import (
    AiAnalyst,
    compute_factor_chips,
)


def test_compute_factor_chips():
    factor_ranks = {
        "quality": 0.95,
        "momentum": 0.92,
        "value": 0.65,
        "growth": 0.88,
    }
    chips = compute_factor_chips(factor_ranks, top_n=2)
    assert len(chips) == 2
    assert chips[0].factor == "quality"
    assert chips[0].label == "Buffett Kalite"
    assert chips[0].score_pct == 95.0
    assert chips[1].factor == "momentum"
    assert chips[1].label == "Momentum"
    assert chips[1].score_pct == 92.0


def test_ai_analyst_rule_based_thesis_generation():
    analyst = AiAnalyst(api_key=None)  # Force rule-based

    thesis_new = analyst.generate_thesis(
        ticker="AAPL",
        company_name="Apple Inc.",
        sector="Computers",
        composite_score=0.94,
        entry_price=220.0,
        target_price=260.0,
        stop_price=185.0,
        factor_ranks={"quality": 0.96, "momentum": 0.92},
        is_incumbent=False,
    )

    assert "Apple Inc." in thesis_new
    assert "AAPL" in thesis_new
    assert "$260.00" in thesis_new
    assert "$185.00" in thesis_new
    assert "Buffett Kalite" in thesis_new


def test_ai_analyst_incumbent_thesis_generation():
    analyst = AiAnalyst(api_key=None)

    thesis_inc = analyst.generate_thesis(
        ticker="MSFT",
        company_name="Microsoft Corporation",
        sector="Software",
        composite_score=0.90,
        entry_price=410.0,
        target_price=495.0,
        stop_price=365.0,
        factor_ranks={"quality": 0.94, "momentum": 0.85},
        is_incumbent=True,
    )

    assert "hold-band" in thesis_inc
    assert "tutulmaya devam ediyor" in thesis_inc
    assert "$495.00" in thesis_inc
    assert "$365.00" in thesis_inc
