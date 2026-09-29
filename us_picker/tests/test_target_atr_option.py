"""#11: opt-in ATR expected-move target for DCF-less names (2026-07-07).

Verifies the default is UNCHANGED (score-implied) and the opt-in path produces
a volatility-scaled target — no DB needed (pure target math on _cfg).
"""

from datetime import date

from us_picker.portfolio.selector import PortfolioSelector


def _candidate(score=80.0):
    return {"company_id": 1, "ticker": "TEST", "score": score, "dcf_mos": None}


def test_default_target_is_score_implied():
    sel = PortfolioSelector(scoring_date=date(2026, 1, 5), strategy_variant="index_aware")
    target, source = sel._compute_target_price_with_source(_candidate(80.0), 100.0, atr=5.0)
    # upside = max(0.10, 0.80 * 0.30) = 0.24 -> 124
    assert source == "SCORE_IMPLIED"
    assert target == 124.0


def test_atr_expected_move_when_enabled():
    sel = PortfolioSelector(
        scoring_date=date(2026, 1, 5),
        strategy_variant="index_aware",
        selection_overrides={"target_expected_move": {"enabled": True, "atr_multiple": 3.0}},
    )
    target, source = sel._compute_target_price_with_source(_candidate(80.0), 100.0, atr=5.0)
    # entry 100 + 3 * ATR 5 = 115
    assert source == "ATR_EXPECTED_MOVE"
    assert target == 115.0


def test_atr_option_ignored_without_atr():
    sel = PortfolioSelector(
        scoring_date=date(2026, 1, 5),
        strategy_variant="index_aware",
        selection_overrides={"target_expected_move": {"enabled": True, "atr_multiple": 3.0}},
    )
    # No ATR available -> falls through to score-implied.
    target, source = sel._compute_target_price_with_source(_candidate(80.0), 100.0, atr=None)
    assert source == "SCORE_IMPLIED"
    assert target == 124.0


def test_dcf_path_still_wins():
    sel = PortfolioSelector(
        scoring_date=date(2026, 1, 5),
        strategy_variant="index_aware",
        selection_overrides={"target_expected_move": {"enabled": True, "atr_multiple": 3.0}},
    )
    cand = _candidate(80.0)
    cand["dcf_mos"] = 20.0  # 20% margin of safety -> intrinsic = 100/0.8 = 125
    target, source = sel._compute_target_price_with_source(cand, 100.0, atr=5.0)
    assert source == "DCF_INTRINSIC"
    assert target == 125.0
