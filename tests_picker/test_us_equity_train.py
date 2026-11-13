import numpy as np
import pandas as pd
import pytest

from scripts_picker.us_equity_train import Portfolio, factors_for_signal


def test_signal_ignores_unpublished_results_and_future_prices_and_splits():
    dates = pd.bdate_range("2020-01-01", periods=255)
    signal = dates[252]
    metadata = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "cik": [1, 2],
            "sic_group": ["35", "50"],
        },
        index=[1, 2],
    )
    memberships = pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "start_date": [dates[0], dates[0]],
            "end_date": [pd.NaT, pd.NaT],
        }
    )
    closes = pd.DataFrame({1: 10.0, 2: 10.0}, index=dates)
    closes.loc[dates[231], 2] = 20
    financials = pd.DataFrame(
        {
            "company_id": [1, 2, 1],
            "period_end": [dates[200], dates[200], dates[240]],
            "publication_date": [dates[220], dates[220], dates[253]],
            "roa_adjusted": [0.1, 0.2, 10],
            "eps_adjusted": [1, 3, 10],
        }
    )
    splits = pd.DataFrame(
        {
            "company_id": [2, 1],
            "action_date": [signal, dates[253]],
            "adjustment_factor": [2, 10],
        }
    )
    adv = pd.DataFrame(30_000_000, index=dates, columns=[1, 2])
    ranks = factors_for_signal(
        signal, closes, closes, adv, metadata, memberships, financials, splits
    )
    for factor in ("quality", "value", "momentum"):
        assert ranks.loc[2, factor] == 1
        assert ranks.loc[1, factor] == 0.5

    closes.loc[dates[253], 1] = 500
    closes.loc[signal, 1] = 10
    financials.loc[2, "roa_adjusted"] = 100
    assert ranks.equals(
        factors_for_signal(
            signal,
            closes,
            closes,
            adv,
            metadata,
            memberships,
            financials,
            splits,
        )
    )


def test_missing_open_never_sells_at_stale_mark_or_selects_a_substitute():
    sample = Portfolio()
    prices = pd.Series({1: 10.0, 2: 10.0})
    inputs = (prices, prices, pd.Series(dtype=float), pd.Series(dtype=float))
    assert sample.value_at_open(*inputs, targets=[1], cost_bps=20) == pytest.approx(1 / 1.002)
    assert sample.cash == pytest.approx(0)
    no_market = pd.Series({1: np.nan, 2: 10.0})
    marks = pd.Series({1: 12.0, 2: 10.0})
    nav = sample.value_at_open(
        marks,
        no_market,
        pd.Series(dtype=float),
        pd.Series({1: 2}),
        targets=[2],
        cost_bps=20,
    )
    assert nav == pytest.approx(1.2 / 1.002)
    assert sample.positions[1] > 0
    assert 2 not in sample.positions
    assert sample.greatest_mark_staleness_sessions == 2
