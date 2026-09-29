"""B1-P2: risk-adjusted sizing report math (advisor review 2026-07-07)."""

import pytest

from us_picker.portfolio.sizing import inverse_vol_weights, risk_parity_report


def test_inverse_vol_weights_favor_low_vol():
    weights = inverse_vol_weights({"LOW": 0.10, "HIGH": 0.40})
    assert weights["LOW"] > weights["HIGH"]
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)
    # 1/0.10 : 1/0.40 = 4 : 1 -> 0.8 / 0.2
    assert weights["LOW"] == pytest.approx(0.8, abs=1e-9)
    assert weights["HIGH"] == pytest.approx(0.2, abs=1e-9)


def test_inverse_vol_missing_falls_back_to_median():
    weights = inverse_vol_weights({"A": 0.20, "B": 0.20, "C": None})
    # C uses median vol (0.20) -> all equal.
    assert weights["C"] == pytest.approx(weights["A"], abs=1e-9)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)


def test_inverse_vol_all_missing_is_equal():
    weights = inverse_vol_weights({"A": None, "B": 0.0})
    assert weights["A"] == pytest.approx(0.5, abs=1e-9)
    assert weights["B"] == pytest.approx(0.5, abs=1e-9)


def test_max_weight_cap_redistributes():
    weights = inverse_vol_weights(
        {"A": 0.05, "B": 0.40, "C": 0.40}, max_weight=0.5
    )
    assert weights["A"] <= 0.5 + 1e-9
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)


def test_risk_parity_report_shape():
    rows = risk_parity_report(
        [{"ticker": "X", "volatility": 0.10}, {"ticker": "Y", "volatility": 0.30}]
    )
    assert {r["ticker"] for r in rows} == {"X", "Y"}
    for r in rows:
        assert r["equal_weight_pct"] == pytest.approx(50.0, abs=1e-6)
        assert "risk_weight_pct" in r and "delta_pct" in r
    # Low-vol X is over-weighted vs equal, high-vol Y under-weighted.
    x = next(r for r in rows if r["ticker"] == "X")
    y = next(r for r in rows if r["ticker"] == "Y")
    assert x["delta_pct"] > 0 > y["delta_pct"]


def test_risk_parity_report_empty():
    assert risk_parity_report([]) == []
