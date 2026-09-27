"""Tests for the concentrated 5-slot portfolio selector and dynamic stops."""

from __future__ import annotations

from decimal import Decimal

import pytest

from usinv.portfolio import (
    Candidate,
    ConcentratedConfig,
    Holding,
    SelectionError,
    calculate_stop_and_target,
    select_concentrated_portfolio,
)

D = Decimal


def _cand(
    security_id: str,
    ticker: str,
    sector: str,
    composite: float,
    *,
    adv: str = "10000000",
    eligible: bool = True,
) -> Candidate:
    return Candidate(
        security_id=security_id,
        ticker=ticker,
        size_bucket="core",
        ff12_group=sector,
        bucket_percentile=composite,
        composite=composite,
        dollar_volume_21d=D(adv),
        eligible=eligible,
    )


def test_concentrated_config_validation():
    cfg = ConcentratedConfig(target_slots=5, max_per_sector=2)
    assert cfg.target_slots == 5
    assert cfg.max_per_sector == 2

    with pytest.raises(SelectionError):
        ConcentratedConfig(target_slots=0)

    with pytest.raises(SelectionError):
        ConcentratedConfig(target_slots=5, max_per_sector=6)

    with pytest.raises(SelectionError):
        ConcentratedConfig(incumbent_turnover_threshold=0.9)


def test_calculate_stop_and_target():
    entry = 100.0

    # With ATR (unclamped between 10% and 25%)
    stop, target = calculate_stop_and_target(entry, 0.90, atr=7.0)
    # Stop = 100 - (7.0 * 2.0) = 86.0
    assert stop == 86.0
    # Target = 100 * (1 + 0.90 * 0.25) = 122.5
    assert target == 122.5

    # Stop clamped to min stop 10%
    stop_clamped_min, _ = calculate_stop_and_target(entry, 0.90, atr=1.0)
    assert stop_clamped_min == 90.0  # 10% minimum stop

    # Stop clamped to max stop 25%
    stop_clamped_max, _ = calculate_stop_and_target(entry, 0.90, atr=20.0)
    assert stop_clamped_max == 75.0  # 25% maximum stop

    # With DCF Margin of Safety
    _, target_dcf = calculate_stop_and_target(entry, 0.90, dcf_margin_of_safety=0.20)
    # 100 / (1 - 0.20) = 125.0
    assert target_dcf == 125.0


def test_select_concentrated_picks_5_slots():
    candidates = (
        _cand("SEC:1", "AAPL", "tech", 0.95),
        _cand("SEC:2", "NVDA", "tech", 0.94),
        _cand("SEC:3", "MSFT", "software", 0.92),
        _cand("SEC:4", "AMZN", "retail", 0.90),
        _cand("SEC:5", "LLY", "health", 0.88),
        _cand("SEC:6", "GOOGL", "tech", 0.86),  # tech cap will skip this
        _cand("SEC:7", "JNJ", "health", 0.84),
    )
    holdings = ()
    prices = {"SEC:1": 220.0, "SEC:2": 120.0, "SEC:3": 430.0, "SEC:4": 185.0, "SEC:5": 900.0}

    res = select_concentrated_portfolio(
        candidates,
        holdings,
        prices,
        config=ConcentratedConfig(target_slots=5, max_per_sector=2),
    )

    assert len(res.picks) == 5
    tickers = [p.ticker for p in res.picks]
    # AAPL, NVDA take 2 tech slots. GOOGL (tech) skipped due to sector cap.
    assert tickers == ["AAPL", "NVDA", "MSFT", "AMZN", "LLY"]
    assert res.entry_security_ids == ("SEC:1", "SEC:2", "SEC:3", "SEC:4", "SEC:5")
    assert res.retained_security_ids == ()


def test_incumbent_retention_buffer():
    # Incumbent held with score 0.85
    # Challenger has 0.90 (< 15% better: 0.85 * 1.15 = 0.9775)
    candidates = (
        _cand("SEC:NEW", "NEW", "fin", 0.90),
        _cand("SEC:HELD", "HELD", "tech", 0.85),
    )
    holdings = (Holding("P1", "SEC:HELD"),)
    prices = {"SEC:HELD": 100.0, "SEC:NEW": 50.0}

    # Target 1 slot
    res = select_concentrated_portfolio(
        candidates,
        holdings,
        prices,
        config=ConcentratedConfig(
            target_slots=1, max_per_sector=1, incumbent_turnover_threshold=1.15
        ),
    )

    # Incumbent should be retained because 0.90 is NOT > 0.85 * 1.15
    assert len(res.picks) == 1
    assert res.picks[0].ticker == "HELD"
    assert res.picks[0].is_incumbent is True
    assert res.retained_security_ids == ("SEC:HELD",)


def test_incumbent_replaced_when_challenger_is_vastly_superior():
    # Incumbent held with score 0.70
    # Challenger has 0.95 (0.95 > 0.70 * 1.15 = 0.805)
    candidates = (
        _cand("SEC:NEW", "NEW", "fin", 0.95),
        _cand("SEC:HELD", "HELD", "tech", 0.70),
    )
    holdings = (Holding("P1", "SEC:HELD"),)
    prices = {"SEC:HELD": 100.0, "SEC:NEW": 50.0}

    res = select_concentrated_portfolio(
        candidates,
        holdings,
        prices,
        config=ConcentratedConfig(
            target_slots=1, max_per_sector=1, incumbent_turnover_threshold=1.15
        ),
    )

    # Incumbent should be replaced
    assert len(res.picks) == 1
    assert res.picks[0].ticker == "NEW"
    assert res.picks[0].is_incumbent is False
    assert res.retained_security_ids == ()
    assert res.replaced_exit_security_ids == ("SEC:HELD",)


def test_forced_exit_always_executed():
    candidates = (
        _cand("SEC:BAD", "BAD", "tech", 0.99),
        _cand("SEC:GOOD", "GOOD", "health", 0.85),
    )
    holdings = (Holding("P1", "SEC:BAD"),)
    prices = {"SEC:BAD": 100.0, "SEC:GOOD": 50.0}

    res = select_concentrated_portfolio(
        candidates,
        holdings,
        prices,
        config=ConcentratedConfig(target_slots=1, max_per_sector=1),
        forced_exit_security_ids=frozenset({"SEC:BAD"}),
    )

    assert len(res.picks) == 1
    assert res.picks[0].ticker == "GOOD"
    assert res.forced_exit_security_ids == ("SEC:BAD",)
