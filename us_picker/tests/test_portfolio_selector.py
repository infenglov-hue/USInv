"""Regression tests for the portfolio selector."""

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    DailyPrice,
    PortfolioSelection,
    ScoringResult,
)
from us_picker.portfolio.cash_signal import CashSignalResult
from us_picker.portfolio.selector import (
    PortfolioSelector,
    _TARGET_SOURCE_DCF,
    _TARGET_SOURCE_SCORE,
)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Session_ = sessionmaker(bind=engine)
    sess = Session_()
    yield sess
    sess.close()


class TestPortfolioSelector:
    def _add_company(
        self,
        session,
        ticker: str,
        sector_bist: str,
        sector_custom: str | None = None,
        company_type: str = "OPERATING",
    ) -> int:
        company = Company(
            ticker=ticker,
            name=ticker,
            company_type=company_type,
            sector_bist=sector_bist,
            sector_custom=sector_custom,
            is_active=True,
        )
        session.add(company)
        session.flush()
        return company.id

    def _add_price(self, session, company_id: int, as_of: date, close: float) -> None:
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

    def _add_score(
        self,
        session,
        company_id: int,
        as_of: date,
        composite_alpha: float,
        technical_score: float = 80.0,
        dcf_margin_of_safety_pct: float = 20.0,
    ) -> None:
        session.add(
            ScoringResult(
                company_id=company_id,
                scoring_date=as_of,
                model_used="OPERATING",
                composite_alpha=composite_alpha,
                technical_score=technical_score,
                dcf_margin_of_safety_pct=dcf_margin_of_safety_pct,
                data_completeness=100.0,
            )
        )

    def test_sector_cap_uses_bist_sector_when_custom_sector_missing(self, session, monkeypatch):
        """Selector should still diversify when sector_custom is absent in DB rows."""
        as_of = date(2026, 2, 1)
        company_ids = [
            self._add_company(session, "AAA1", "Technology"),
            self._add_company(session, "AAA2", "Technology"),
            self._add_company(session, "AAA3", "Technology"),
            self._add_company(session, "BBB1", "Industrial"),
        ]
        scores = [95.0, 90.0, 85.0, 80.0]
        for cid, score in zip(company_ids, scores):
            self._add_price(session, cid, as_of, close=10.0 + score)
            self._add_score(session, cid, as_of, composite_alpha=score)
        session.commit()

        selector = PortfolioSelector(scoring_date=as_of, strategy_variant="classic")
        monkeypatch.setattr(
            selector._universe,
            "get_universe",
            lambda portfolio, db_session: company_ids,
        )

        picks = selector.select("ALPHA", session)
        picked_tickers = {pick["ticker"] for pick in picks}

        # Phase 3 tightening: max_per_sector=2. Of the 3 Tech candidates only
        # the top 2 by composite score survive the sector cap; the single
        # Industrial candidate also makes it in.
        assert len(picks) == 3
        assert picked_tickers == {"AAA1", "AAA2", "BBB1"}
        assert {pick["target_source"] for pick in picks} == {_TARGET_SOURCE_DCF}


def _make_cash_signal(as_of: date) -> CashSignalResult:
    return CashSignalResult(
        date=as_of,
        market_regime="RISK_ON",
        macro_regime="NEUTRAL",
        raw_signal=0,
        target_state="NORMAL",
        state="NORMAL",
        cash_pct=0.0,
        days_in_state=1,
        last_transition_date=None,
        transitioned_today=False,
        notes="test",
    )


def _add_company(session, ticker: str, sector: str = "Industrial") -> int:
    company = Company(
        ticker=ticker,
        name=ticker,
        company_type="OPERATING",
        sector_bist=sector,
        is_active=True,
    )
    session.add(company)
    session.flush()
    return company.id


class TestSelectAndStoreEmptyGuard:
    """2026-07-02 MAJOR 1 regression: an empty pick list (failed universe or
    scoring lookup, or an exception swallowed by select_all) must never
    liquidate the existing open positions."""

    def test_empty_picks_keep_open_positions(self, session):
        as_of = date(2026, 2, 1)
        cid = _add_company(session, "HOLD1")
        session.add(
            PortfolioSelection(
                portfolio="ALPHA",
                selection_date=date(2026, 1, 5),
                company_id=cid,
                entry_price=50.0,
                composite_score=90.0,
            )
        )
        session.commit()

        selector = PortfolioSelector(scoring_date=as_of, strategy_variant="classic")
        selector.select_all = MagicMock(return_value={"alpha": []})

        result = selector.select_and_store(
            session, cash_signal=_make_cash_signal(as_of)
        )

        assert result == {"alpha": []}
        row = session.query(PortfolioSelection).one()
        assert row.exit_date is None, (
            "Empty picks must not exit open positions — that would liquidate "
            "the portfolio on a transient scoring failure."
        )
        assert row.exit_reason is None

    def test_non_empty_picks_still_rebalance_old_positions(self, session):
        as_of = date(2026, 2, 1)
        old_cid = _add_company(session, "OLD1")
        new_cid = _add_company(session, "NEW1")
        session.add(
            DailyPrice(
                company_id=old_cid,
                date=as_of,
                close=60.0,
                adjusted_close=60.0,
                high=60.0,
                low=60.0,
                volume=1000,
            )
        )
        session.add(
            PortfolioSelection(
                portfolio="ALPHA",
                selection_date=date(2026, 1, 5),
                company_id=old_cid,
                entry_price=50.0,
                composite_score=90.0,
            )
        )
        session.commit()

        new_pick = {
            "company_id": new_cid,
            "ticker": "NEW1",
            "score": 88.0,
            "rank": 1,
            "entry_price": 100.0,
            "target_price": 120.0,
            "target_source": _TARGET_SOURCE_SCORE,
            "stop_loss": 90.0,
            "dcf_mos": None,
            "reason_top_factors": [],
        }
        selector = PortfolioSelector(scoring_date=as_of, strategy_variant="classic")
        selector.select_all = MagicMock(return_value={"alpha": [new_pick]})

        selector.select_and_store(session, cash_signal=_make_cash_signal(as_of))

        old_row = (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.company_id == old_cid)
            .one()
        )
        assert old_row.exit_date == as_of
        assert old_row.exit_reason == "REBALANCE"
        assert old_row.exit_price == pytest.approx(60.0)

        new_row = (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.company_id == new_cid)
            .one()
        )
        assert new_row.selection_date == as_of
        assert new_row.exit_date is None
        assert new_row.weight == pytest.approx(1.0)
        assert new_row.target_source == _TARGET_SOURCE_SCORE
        assert new_row.cycle_ref_date == as_of
        assert new_row.cycle_ref_price == pytest.approx(100.0)
        assert new_row.highest_close == pytest.approx(100.0)


def _pick(company_id: int, ticker: str, entry: float, stop: float, score: float = 90.0) -> dict:
    return {
        "company_id": company_id,
        "ticker": ticker,
        "score": score,
        "rank": 1,
        "entry_price": entry,
        "target_price": entry * 1.3,
        "target_source": _TARGET_SOURCE_SCORE,
        "stop_loss": stop,
        "dcf_mos": None,
        "reason_top_factors": [],
    }


class TestPositionContinuity:
    """B1 (2026-07-05): a position kept across rotations stays in ONE row —
    the original entry price / selection date are the cost basis; only
    rotation-scoped fields refresh, and the stop only ratchets up."""

    ENTRY_DATE = date(2026, 6, 15)
    ROTATION = date(2026, 6, 29)

    def _seed_open_position(self, session, ticker="KEEP1", entry=50.0, stop=45.0):
        cid = _add_company(session, ticker)
        session.add(
            PortfolioSelection(
                portfolio="ALPHA",
                selection_date=self.ENTRY_DATE,
                company_id=cid,
                entry_price=entry,
                composite_score=90.0,
                stop_loss_price=stop,
                cycle_ref_date=self.ENTRY_DATE,
                cycle_ref_price=entry,
                highest_close=entry,
            )
        )
        session.commit()
        return cid

    def _run(self, session, picks):
        selector = PortfolioSelector(
            scoring_date=self.ROTATION, strategy_variant="classic"
        )
        selector.select_all = MagicMock(return_value={"alpha": picks})
        return selector.select_and_store(
            session, cash_signal=_make_cash_signal(self.ROTATION)
        )

    def test_incumbent_keeps_single_row_and_original_entry(self, session):
        cid = self._seed_open_position(session, entry=50.0, stop=45.0)

        self._run(session, [_pick(cid, "KEEP1", entry=60.0, stop=52.0, score=93.0)])

        rows = (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.company_id == cid)
            .all()
        )
        assert len(rows) == 1, "Held position must stay in a single row"
        row = rows[0]
        assert row.exit_date is None
        assert row.entry_price == pytest.approx(50.0), "Cost basis must survive rotation"
        assert row.selection_date == self.ENTRY_DATE
        assert row.composite_score == pytest.approx(93.0)
        assert row.cycle_ref_date == self.ROTATION
        assert row.cycle_ref_price == pytest.approx(60.0)
        assert row.stop_loss_price == pytest.approx(52.0), "Higher new stop ratchets up"
        assert row.target_source == _TARGET_SOURCE_SCORE

    def test_incumbent_stop_never_lowered_on_rotation(self, session):
        cid = self._seed_open_position(session, entry=50.0, stop=55.0)

        self._run(session, [_pick(cid, "KEEP1", entry=60.0, stop=48.0)])

        row = session.query(PortfolioSelection).one()
        assert row.stop_loss_price == pytest.approx(55.0), (
            "Rotation must never re-anchor the stop DOWN — that was the "
            "pre-B1 bug that made stops effectively decorative."
        )

    def test_dropped_position_exits_with_pnl_from_original_entry(self, session):
        kept = self._seed_open_position(session, ticker="KEEP1", entry=50.0)
        dropped = _add_company(session, "DROP1")
        session.add(
            PortfolioSelection(
                portfolio="ALPHA",
                selection_date=self.ENTRY_DATE,
                company_id=dropped,
                entry_price=40.0,
                composite_score=85.0,
            )
        )
        session.add(
            DailyPrice(
                company_id=dropped,
                date=self.ROTATION,
                close=44.0,
                adjusted_close=44.0,
                high=44.0,
                low=44.0,
                volume=1000,
            )
        )
        session.commit()

        self._run(session, [_pick(kept, "KEEP1", entry=60.0, stop=52.0)])

        dropped_row = (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.company_id == dropped)
            .one()
        )
        assert dropped_row.exit_date == self.ROTATION
        assert dropped_row.exit_reason == "REBALANCE"
        assert dropped_row.return_pct == pytest.approx(10.0), (
            "Exit P&L must be measured from the ORIGINAL entry (40 -> 44)"
        )
        assert dropped_row.holding_days == (self.ROTATION - self.ENTRY_DATE).days

    def test_cycle_marks_written_for_all_open_positions(self, session):
        from us_picker.db.schema import PortfolioCycleMark

        kept = self._seed_open_position(session, ticker="KEEP1", entry=50.0)
        fresh = _add_company(session, "NEW1")
        session.commit()

        self._run(
            session,
            [
                _pick(kept, "KEEP1", entry=60.0, stop=52.0),
                _pick(fresh, "NEW1", entry=100.0, stop=88.0),
            ],
        )

        marks = (
            session.query(PortfolioCycleMark)
            .filter_by(portfolio="ALPHA", cycle_date=self.ROTATION)
            .all()
        )
        by_company = {m.company_id: m.ref_price for m in marks}
        assert by_company == {
            kept: pytest.approx(60.0),
            fresh: pytest.approx(100.0),
        }

    def test_same_day_rerun_is_idempotent(self, session):
        from us_picker.db.schema import PortfolioCycleMark

        kept = self._seed_open_position(session, ticker="KEEP1", entry=50.0)
        fresh = _add_company(session, "NEW1")
        session.commit()

        picks = [
            _pick(kept, "KEEP1", entry=60.0, stop=52.0),
            _pick(fresh, "NEW1", entry=100.0, stop=88.0),
        ]
        self._run(session, picks)
        self._run(session, picks)  # same-day rerun

        assert (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.exit_date.is_(None))
            .count()
            == 2
        )
        kept_row = (
            session.query(PortfolioSelection)
            .filter(PortfolioSelection.company_id == kept)
            .one()
        )
        assert kept_row.entry_price == pytest.approx(50.0)
        assert (
            session.query(PortfolioCycleMark)
            .filter_by(portfolio="ALPHA", cycle_date=self.ROTATION)
            .count()
            == 2
        )


class TestIndexAwareCorrelationReplacement:
    """2026-07-02 MAJOR 2: correlation reduction now runs on the index-aware
    path, and replacements must respect the non-BIST100 sleeve cap."""

    @staticmethod
    def _candidate(company_id: int, ticker: str, score: float, sector: str, is_bist100: bool) -> dict:
        return {
            "company_id": company_id,
            "ticker": ticker,
            "score": score,
            "base_score": score,
            "sector_custom": sector,
            "sector_key": sector,
            "company_type": "OPERATING",
            "is_bist100": is_bist100,
            "risk_tier": None,
            "free_float_pct": 40.0,
            "dcf_mos": None,
            "data_completeness": 100.0,
            "technical_score": None,
            "above_200ma": None,
            "relative_strength_score": None,
            "factor_scores": {},
        }

    def _add_price_series(self, session, company_id: int, pattern: str) -> None:
        start = date(2025, 12, 1)
        for i in range(40):
            if pattern == "alt":          # 100, 102, 100, 102, ...
                close = 100.0 if i % 2 == 0 else 102.0
            elif pattern == "alt_opp":    # 102, 100, 102, 100, ...
                close = 102.0 if i % 2 == 0 else 100.0
            else:                          # steady +0.5%/day → zero return variance
                close = round(100.0 * (1.005 ** i), 6)
            session.add(
                DailyPrice(
                    company_id=company_id,
                    date=start + timedelta(days=i),
                    close=close,
                    adjusted_close=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    volume=1000,
                )
            )

    def test_replacement_respects_non_bist100_cap(self, session):
        as_of = date(2026, 2, 1)
        a = _add_company(session, "AAA")
        b = _add_company(session, "BBB")
        d = _add_company(session, "DDD")
        c_non_bist = _add_company(session, "CCC")
        c_bist = _add_company(session, "CC2")

        # A and B are perfectly correlated; D has zero return variance
        # (corr = nan → ignored); both replacement candidates are perfectly
        # anti-correlated with A, so only the sleeve cap separates them.
        self._add_price_series(session, a, "alt")
        self._add_price_series(session, b, "alt")
        self._add_price_series(session, d, "steady")
        self._add_price_series(session, c_non_bist, "alt_opp")
        self._add_price_series(session, c_bist, "alt_opp")
        session.commit()

        selector = PortfolioSelector(scoring_date=as_of, strategy_variant="index_aware")
        cand_a = self._candidate(a, "AAA", 95.0, "S1", is_bist100=False)
        cand_b = self._candidate(b, "BBB", 90.0, "S2", is_bist100=False)
        cand_d = self._candidate(d, "DDD", 85.0, "S3", is_bist100=False)
        cand_c = self._candidate(c_non_bist, "CCC", 80.0, "S4", is_bist100=False)
        cand_c2 = self._candidate(c_bist, "CC2", 78.0, "S5", is_bist100=True)

        picks = [
            selector._build_pick(cand_a, 1, session),
            selector._build_pick(cand_b, 2, session),
            selector._build_pick(cand_d, 3, session),
        ]
        assert all(p is not None for p in picks)
        sector_counts = {"S1": 1, "S2": 1, "S3": 1}

        reduced = selector._reduce_correlation(
            picks,
            [cand_c, cand_c2],
            sector_counts,
            0,
            0.70,
            session,
            index_aware=True,
        )
        tickers = {p["ticker"] for p in reduced}

        # B (lower-scored half of the correlated pair) is replaced. CCC would
        # be the third non-BIST100 name — the sleeve cap (max 2) rejects it,
        # so the BIST100 candidate CC2 takes the slot instead.
        assert tickers == {"AAA", "CC2", "DDD"}
