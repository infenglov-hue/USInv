"""Tests for ExitRuleChecker (stop-loss, target, thesis-breaker)."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    DailyPrice,
    InsiderTransaction,
    PortfolioSelection,
)
from us_picker.portfolio.exit_rules import ExitRuleChecker


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


def _add_position(
    session,
    ticker: str = "XPOS",
    entry_price: float = 100.0,
    current_price: float = 100.0,
    stop_loss_price: float | None = None,
    target_price: float | None = None,
) -> Company:
    company = Company(
        ticker=ticker, name=f"{ticker} A.S.",
        company_type="OPERATING", is_active=True,
    )
    session.add(company)
    session.flush()

    session.add(DailyPrice(
        company_id=company.id, date=date(2026, 7, 1), close=current_price,
    ))
    session.add(PortfolioSelection(
        company_id=company.id,
        portfolio="ALPHA",
        selection_date=date(2026, 6, 22),
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        target_price=target_price,
    ))
    session.flush()
    return company


class TestStopLossAndTarget:
    def test_stop_loss_triggers(self, session):
        _add_position(session, entry_price=100.0, current_price=81.0)
        signals = ExitRuleChecker(session).check_exits()
        assert len(signals) == 1
        assert signals[0]["reason"] == "STOP_LOSS"

    def test_no_signal_when_price_holds(self, session):
        _add_position(session, entry_price=100.0, current_price=95.0)
        signals = ExitRuleChecker(session).check_exits()
        assert signals == []

    def test_target_triggers(self, session):
        _add_position(
            session, entry_price=100.0, current_price=130.0, target_price=125.0,
        )
        signals = ExitRuleChecker(session).check_exits()
        assert len(signals) == 1
        assert signals[0]["reason"] == "TARGET"


class TestInsiderThreshold:
    """Thesis-breaker threshold = max(5M TRY, 0.5% of market cap)."""

    def _add_insider_sell(self, session, company_id: int, value_try: float):
        session.add(InsiderTransaction(
            company_id=company_id,
            disclosure_date=date(2026, 6, 25),
            transaction_type="SELL",
            total_value_try=value_try,
        ))
        session.flush()

    def test_floor_applies_when_market_cap_unknown(self, session):
        """No balance sheet data -> market cap None -> 5M TRY floor."""
        company = _add_position(session, current_price=95.0)
        self._add_insider_sell(session, company.id, 6_000_000.0)

        signals = ExitRuleChecker(session).check_exits()
        assert len(signals) == 1
        assert signals[0]["reason"] == "THESIS_BREAKER"

    def test_below_floor_does_not_trigger(self, session):
        company = _add_position(session, current_price=95.0)
        self._add_insider_sell(session, company.id, 4_000_000.0)

        signals = ExitRuleChecker(session).check_exits()
        assert signals == []

    def test_threshold_scales_with_market_cap(self, session):
        """Large cap: 6M TRY selling is noise if 0.5% of mcap is higher."""
        company = _add_position(session, current_price=95.0)
        self._add_insider_sell(session, company.id, 6_000_000.0)

        checker = ExitRuleChecker(session)

        # Simulate a resolvable 10B TRY market cap without seeding a full
        # balance sheet: patch the classifier lookup.
        class _FakeClassifier:
            def compute_market_cap(self, company_id, sess, scoring_date=None):
                return 10_000_000_000.0

        checker._risk_classifier = _FakeClassifier()

        # threshold = max(5M, 10B * 0.005) = 50M -> 6M selling is ignored
        assert checker._insider_threshold(company.id) == 50_000_000.0
        assert checker.check_exits() == []


class TestTrailingStop:
    """B1: daily trailing-stop ratchet — stop follows the highest close up,
    never down; distance = clamp(2*ATR/price, 10%, 25%)."""

    def test_stop_ratchets_up_with_price(self, session, monkeypatch):
        company = _add_position(
            session, entry_price=100.0, current_price=120.0, stop_loss_price=90.0,
        )
        from us_picker.scoring.factors.technical import TechnicalScorer
        # ATR 6 on price 120 -> raw trail 10%, clamped to the production
        # 20% floor -> stop = 120*0.80 = 96
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: 6.0,
        )

        checker = ExitRuleChecker(session)
        raised = checker.update_trailing_stops(force_enabled=True)

        row = session.query(PortfolioSelection).one()
        assert raised == 1
        assert row.highest_close == pytest.approx(120.0)
        assert row.stop_loss_price == pytest.approx(96.0)

    def test_stop_never_lowered_when_price_falls(self, session, monkeypatch):
        company = _add_position(
            session, entry_price=100.0, current_price=95.0, stop_loss_price=90.0,
        )
        row = session.query(PortfolioSelection).one()
        row.highest_close = 110.0
        session.flush()

        from us_picker.scoring.factors.technical import TechnicalScorer
        # trail on high 110 with 25% floor-clamped ATR: candidate 82.5 < 90
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: 20.0,
        )

        checker = ExitRuleChecker(session)
        raised = checker.update_trailing_stops(force_enabled=True)

        row = session.query(PortfolioSelection).one()
        assert raised == 0
        assert row.stop_loss_price == pytest.approx(90.0)
        assert row.highest_close == pytest.approx(110.0), "High-water mark persists"

    def test_atr_clamp_and_fallback(self, session, monkeypatch):
        company = _add_position(
            session, entry_price=100.0, current_price=200.0, stop_loss_price=90.0,
        )
        from us_picker.scoring.factors.technical import TechnicalScorer
        # Tiny ATR -> clamped to the 20% floor: stop = 200*0.80 = 160
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: 0.5,
        )
        checker = ExitRuleChecker(session)
        checker.update_trailing_stops(force_enabled=True)
        assert session.query(PortfolioSelection).one().stop_loss_price == pytest.approx(160.0)

    def test_no_atr_uses_max_trail_distance(self, session, monkeypatch):
        company = _add_position(
            session, entry_price=100.0, current_price=200.0, stop_loss_price=90.0,
        )
        from us_picker.scoring.factors.technical import TechnicalScorer
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: None,
        )
        checker = ExitRuleChecker(session)
        checker.update_trailing_stops(force_enabled=True)
        # No ATR -> conservative 35% max trail: 200*0.65 = 130
        assert session.query(PortfolioSelection).one().stop_loss_price == pytest.approx(130.0)

    def test_trailing_then_exit_fires_same_run(self, session, monkeypatch):
        """A raised stop must be effective in the SAME check-exits run."""
        company = _add_position(
            session, entry_price=100.0, current_price=100.0, stop_loss_price=70.0,
        )
        row = session.query(PortfolioSelection).one()
        row.highest_close = 140.0  # prior peak
        session.flush()

        from us_picker.scoring.factors.technical import TechnicalScorer
        # trail 10% on high 140 -> stop 126 > current 100 -> STOP_LOSS signal
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: 5.0,
        )

        checker = ExitRuleChecker(session)
        checker.update_trailing_stops(force_enabled=True)
        signals = checker.check_exits()
        assert len(signals) == 1
        assert signals[0]["reason"] == "STOP_LOSS"


    def test_disabled_trailing_tracks_high_water_but_never_raises(self, session, monkeypatch):
        """With trailing disabled: highest_close bookkeeping still runs,
        the stop is never touched."""
        _add_position(
            session, entry_price=100.0, current_price=150.0, stop_loss_price=90.0,
        )
        from us_picker.scoring.factors.technical import TechnicalScorer
        monkeypatch.setattr(
            TechnicalScorer, "calculate_atr",
            lambda self, cid, sess, scoring_date=None: 6.0,
        )

        checker = ExitRuleChecker(session)
        raised = checker.update_trailing_stops(force_enabled=False)

        row = session.query(PortfolioSelection).one()
        assert raised == 0
        assert row.stop_loss_price == pytest.approx(90.0)
        assert row.highest_close == pytest.approx(150.0)

    def test_production_config_uses_wide_trail_clamp(self):
        """2026-07-05 A/B: trailing ships ENABLED with the 20-35% clamp.
        A tighter clamp (10-25%) collapsed the backtest (+852% vs +2958%) —
        pin the band so a casual YAML edit can't reintroduce it."""
        from us_picker.portfolio.exit_rules import _load_trailing_config

        cfg = _load_trailing_config()
        assert cfg["enabled"] is True
        assert cfg["min_pct"] == pytest.approx(0.20)
        assert cfg["max_pct"] == pytest.approx(0.35)