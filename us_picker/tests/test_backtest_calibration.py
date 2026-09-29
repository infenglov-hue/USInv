"""Backtest calibration support tests (A1/C1, 2026-07-04).

Covers the three pieces added for the momentum-skip A/B and the
rotation-frequency/turnover grid:

* ``_build_rebalance_dates`` — every-Nth-Monday cadence.
* ``_scoring_universe`` — INDEX/mock rows must never enter point-in-time
  scoring. They are ``is_active=0`` with no ``delisting_date``, so the as-of
  proxy resurrects them at historical dates; a single model_type INDEX row
  aborts ``compose_all`` and the whole date is left without composites.
* ``PortfolioSelector(selection_overrides=...)`` — grid runs override
  ``turnover_threshold`` (and nested index_aware keys) without YAML edits.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.backtest.engine import _build_rebalance_dates, _scoring_universe
from us_picker.db.schema import Base, Company, DailyPrice
from us_picker.portfolio.selector import PortfolioSelector


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    yield sess
    sess.close()


def _add_company(
    session,
    ticker: str,
    company_type: str | None = "OPERATING",
    is_active: bool = True,
    delisting_date: date | None = None,
) -> int:
    company = Company(
        ticker=ticker,
        name=ticker,
        company_type=company_type,
        sector_bist="Industrial",
        is_active=is_active,
        delisting_date=delisting_date,
    )
    session.add(company)
    session.flush()
    return company.id


def _add_price(session, company_id: int, as_of: date, close: float = 10.0) -> None:
    session.add(
        DailyPrice(
            company_id=company_id,
            date=as_of,
            close=close,
            adjusted_close=close,
            high=close,
            low=close,
            volume=1000,
        )
    )


class TestBuildRebalanceDates:
    START = date(2018, 3, 19)  # a Monday
    END = date(2018, 5, 14)  # 9 Mondays inclusive

    def test_weekly_keeps_every_monday(self):
        dates = _build_rebalance_dates(self.START, self.END, 1)
        assert len(dates) == 9
        assert dates[0] == self.START
        assert all(d.weekday() == 0 for d in dates)
        assert (dates[1] - dates[0]).days == 7

    def test_biweekly_keeps_every_second_monday(self):
        dates = _build_rebalance_dates(self.START, self.END, 2)
        assert dates == _build_rebalance_dates(self.START, self.END, 1)[::2]
        assert (dates[1] - dates[0]).days == 14

    def test_monthlyish_keeps_every_fourth_monday(self):
        dates = _build_rebalance_dates(self.START, self.END, 4)
        assert dates[0] == self.START
        assert (dates[1] - dates[0]).days == 28

    def test_non_monday_start_aligns_forward(self):
        dates = _build_rebalance_dates(date(2018, 3, 21), self.END, 1)
        assert dates[0] == date(2018, 3, 26)
        assert all(d.weekday() == 0 for d in dates)

    def test_invalid_interval_clamps_to_weekly(self):
        assert _build_rebalance_dates(self.START, self.END, 0) == (
            _build_rebalance_dates(self.START, self.END, 1)
        )

    def test_biweekly_anchor_parity_does_not_depend_on_start_date(self):
        anchor = date(2026, 6, 29)
        dates = _build_rebalance_dates(
            date(2026, 1, 5),
            date(2026, 2, 9),
            2,
            anchor_date=anchor,
        )
        assert dates == [date(2026, 1, 12), date(2026, 1, 26), date(2026, 2, 9)]


class TestScoringUniverse:
    AS_OF = date(2018, 4, 2)

    def test_index_and_mock_rows_excluded(self, session):
        operating = _add_company(session, "REAL")
        index = _add_company(session, "SPY", company_type="INDEX", is_active=False)
        mock = _add_company(session, "MOCK1", is_active=False)
        test_row = _add_company(session, "TESTMACRO", company_type=None, is_active=False)
        # All of them keep printing prices after AS_OF — the as-of proxy
        # alone would resurrect every one of them.
        for cid in (operating, index, mock, test_row):
            _add_price(session, cid, date(2026, 7, 1))
        session.commit()

        ids = {c.id for c in _scoring_universe(session, self.AS_OF)}
        assert ids == {operating}

    def test_since_delisted_company_still_scored(self, session):
        survivor = _add_company(session, "LIVE")
        delisted = _add_company(
            session, "GONE", is_active=False, delisting_date=date(2020, 1, 1)
        )
        session.commit()

        ids = {c.id for c in _scoring_universe(session, self.AS_OF)}
        assert ids == {survivor, delisted}

    def test_null_company_type_kept(self, session):
        untyped = _add_company(session, "RAWCO", company_type=None)
        session.commit()

        ids = {c.id for c in _scoring_universe(session, self.AS_OF)}
        assert untyped in ids


class TestParseSelectionOverrides:
    def test_dotted_keys_nest_and_cast(self):
        from us_picker.cli import _parse_selection_overrides

        parsed = _parse_selection_overrides([
            "index_aware.incumbent_min_score=84",
            "turnover_threshold=0.10",
            "index_aware.min_bist100=3",
        ])
        assert parsed == {
            "index_aware": {"incumbent_min_score": 84, "min_bist100": 3},
            "turnover_threshold": 0.10,
        }

    def test_empty_returns_none(self):
        from us_picker.cli import _parse_selection_overrides

        assert _parse_selection_overrides(()) is None

    def test_missing_equals_raises(self):
        import click
        from us_picker.cli import _parse_selection_overrides

        with pytest.raises(click.BadParameter):
            _parse_selection_overrides(["index_aware.incumbent_min_score"])


class TestSelectionOverrides:
    def test_turnover_threshold_override(self):
        base = PortfolioSelector(scoring_date=date(2026, 7, 1))
        overridden = PortfolioSelector(
            scoring_date=date(2026, 7, 1),
            selection_overrides={"turnover_threshold": 0.15},
        )
        assert base._cfg.get("turnover_threshold") == 0.05
        assert overridden._cfg.get("turnover_threshold") == 0.15

    def test_nested_index_aware_merge_keeps_siblings(self):
        overridden = PortfolioSelector(
            scoring_date=date(2026, 7, 1),
            selection_overrides={"index_aware": {"min_bist100": 3}},
        )
        cfg = overridden._cfg["index_aware"]
        assert cfg["min_bist100"] == 3
        # Sibling keys from thresholds.yaml survive the merge.
        assert cfg["incumbent_min_score"] == 88.0

    def test_no_overrides_is_noop(self):
        base = PortfolioSelector(scoring_date=date(2026, 7, 1))
        explicit_none = PortfolioSelector(
            scoring_date=date(2026, 7, 1), selection_overrides=None
        )
        assert base._cfg == explicit_none._cfg


class TestWeeklyExitTrailing:
    """B1 continuity: _get_weekly_exit ratchets the stop off the highest
    close inside the window and reports state back for the next period."""

    def _bar(self, session, cid, day, o, h, l, c):
        session.add(DailyPrice(
            company_id=cid, date=day, open=o, high=h, low=l, close=c,
            adjusted_close=c, volume=1000,
        ))

    def test_trailing_stop_fires_after_runup_reversal(self, session):
        from us_picker.backtest.engine import BacktestEngine

        cid = _add_company(session, "TRAIL1")
        # Mon: flat 100. Tue: rally to 130 close. Wed: fall to 112 low.
        self._bar(session, cid, date(2026, 6, 1), 100, 101, 99, 100)
        self._bar(session, cid, date(2026, 6, 2), 101, 131, 100, 130)
        self._bar(session, cid, date(2026, 6, 3), 128, 129, 112, 115)
        session.commit()

        engine = BacktestEngine(session)
        trail_state = {"pct": 0.10, "high": 100.0, "stop": 85.0}
        price, exited, reason, exit_day = engine._get_weekly_exit(
            "TRAIL1", date(2026, 6, 1), date(2026, 6, 8),
            stop_loss=85.0, take_profit=None,
            include_start_date=True, trail_state=trail_state,
        )

        # Tue close 130 ratchets stop to 117; Wed low 112 <= 117 -> STOP_LOSS
        assert exited is True
        assert reason == "STOP_LOSS"
        assert exit_day == date(2026, 6, 3)
        assert price == pytest.approx(117.0)
        assert trail_state["high"] == pytest.approx(130.0)

    def test_without_trailing_same_path_survives(self, session):
        from us_picker.backtest.engine import BacktestEngine

        cid = _add_company(session, "TRAIL2")
        self._bar(session, cid, date(2026, 6, 1), 100, 101, 99, 100)
        self._bar(session, cid, date(2026, 6, 2), 101, 131, 100, 130)
        self._bar(session, cid, date(2026, 6, 3), 128, 129, 112, 115)
        session.commit()

        engine = BacktestEngine(session)
        price, exited, reason, _ = engine._get_weekly_exit(
            "TRAIL2", date(2026, 6, 1), date(2026, 6, 8),
            stop_loss=85.0, take_profit=None, include_start_date=True,
        )

        # Static stop 85 never hit -> position survives at last close
        assert exited is False
        assert price == pytest.approx(115.0)

    def test_state_carries_when_no_exit(self, session):
        from us_picker.backtest.engine import BacktestEngine

        cid = _add_company(session, "TRAIL3")
        self._bar(session, cid, date(2026, 6, 1), 100, 101, 99, 100)
        self._bar(session, cid, date(2026, 6, 2), 101, 121, 100, 120)
        session.commit()

        engine = BacktestEngine(session)
        trail_state = {"pct": 0.10, "high": 100.0, "stop": 85.0}
        price, exited, _, _ = engine._get_weekly_exit(
            "TRAIL3", date(2026, 6, 1), date(2026, 6, 8),
            stop_loss=85.0, take_profit=None,
            include_start_date=True, trail_state=trail_state,
        )

        assert exited is False
        assert price == pytest.approx(120.0)
        assert trail_state["high"] == pytest.approx(120.0)
        assert trail_state["stop"] == pytest.approx(108.0)
