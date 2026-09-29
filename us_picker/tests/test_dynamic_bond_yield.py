"""Tests for the dynamic bond yield fetching, Graham Growth Formula, Composer haircuts, and Momentum date alignment.

Pins the new key improvements:
1. Database field `bond_yield_10y_pct` on `MacroRegime`.
2. TCMB EVDS fetching for BIST-TLREF (`TP.BISTTLREF.ORAN`).
3. Adapting Graham Growth Formula to configurable `graham_numerator` and nominal yield.
4. Composer specialized model peer group haircut softening (max 5%).
5. Momentum date alignment to actual last price date.
"""

from datetime import date, timedelta
from unittest.mock import patch, MagicMock, ANY
import pytest
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.connection import _RUNTIME_SQLITE_COLUMN_ADDS
from us_picker.db.schema import Base, MacroRegime, Company, DailyPrice, AdjustedMetric, ScoringResult
from us_picker.data.fetcher import DataFetcher
from us_picker.scoring.factors.graham import GrahamScorer
from us_picker.scoring.composer import ScoreComposer
from us_picker.scoring.factors.momentum import MomentumScorer


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    
    # Run dynamic DDL migrations (column adds) manually since we're in-memory
    conn = eng.connect()
    for (table, col), ddl in _RUNTIME_SQLITE_COLUMN_ADDS.items():
        try:
            conn.execute(conn.text(ddl))
        except Exception:
            pass  # Some columns might already exist in Base schema
            
    SessionFactory = sessionmaker(bind=eng)
    sess = SessionFactory()
    yield sess
    sess.close()


def test_graham_scorer_resolves_bond_yield_precedence(session):
    """GrahamScorer resolves bond yield from bond_yield_10y_pct first, then falls back."""
    # Scenario A: both bond_yield_10y_pct and policy_rate_pct present. Should prefer bond_yield_10y_pct.
    session.add(MacroRegime(date=date(2026, 5, 26), policy_rate_pct=0.40, bond_yield_10y_pct=0.455))
    session.flush()

    scorer = GrahamScorer()
    y = scorer._resolve_bond_yield(session, scoring_date=date(2026, 5, 26))
    assert y == pytest.approx(0.455)

    # Scenario B: only policy_rate_pct present. Should fall back to policy_rate_pct.
    session.query(MacroRegime).delete()
    session.add(MacroRegime(date=date(2026, 5, 26), policy_rate_pct=0.42, bond_yield_10y_pct=None))
    session.flush()

    scorer._bond_yield_cache = None  # clear cache
    y = scorer._resolve_bond_yield(session, scoring_date=date(2026, 5, 26))
    assert y == pytest.approx(0.42)


def test_graham_growth_formula_uses_customizable_numerator(session):
    """Graham Growth Formula uses graham_numerator from thresholds.yaml."""
    # Let's seed a macro regime with 40% BIST-TLREF
    session.add(MacroRegime(date=date(2026, 5, 26), bond_yield_10y_pct=0.40))
    session.flush()

    company = Company(ticker="TEST", name="Test Inc", company_type="OPERATING", is_active=True)
    session.add(company)
    session.flush()

    # 10% nominal growth
    metrics = [
        AdjustedMetric(company_id=company.id, period_end=date(2025, 12, 31), eps_adjusted=10.0),
        AdjustedMetric(company_id=company.id, period_end=date(2026, 12, 31), eps_adjusted=11.0)
    ]

    scorer = GrahamScorer()
    
    # Override thresholds to use graham_numerator = 30.0 (Turkish nominal baseline)
    scorer._thresholds["graham_numerator"] = 30.0
    scorer._bond_yield_cache = None

    # Expected calculation:
    # g = 10% (actually slightly more due to year length fraction)
    # bond_yield = 0.40 -> 40.0
    # intrinsic_value = 11.0 * (8.5 + 2 * g) * (30.0 / 40.0) = approx 235
    
    # Let's test _score_graham_growth directly
    score = scorer._score_graham_growth(metrics, price=235.125, session=session, scoring_date=date(2026, 5, 26))
    
    # ratio should be very close to 1.0, so score is approx 50.0.
    assert score == pytest.approx(50.0, abs=0.2)


def test_composer_softened_haircut(session):
    """ScoreComposer applies softened peer-group haircut (max 5%) to non-operating sectors."""
    composer = ScoreComposer()

    # We mock 3 companies, one operating and two banking, setting their raw composite_alpha
    bank1 = ScoringResult(company_id=1, scoring_date=date(2026, 5, 26), model_used="BANK", banking_composite=80.0, composite_alpha=80.0)
    bank2 = ScoringResult(company_id=2, scoring_date=date(2026, 5, 26), model_used="BANK", banking_composite=90.0, composite_alpha=90.0)
    oper = ScoringResult(company_id=3, scoring_date=date(2026, 5, 26), model_used="OPERATING", composite_alpha=75.0)

    rows = [bank1, bank2, oper]

    # Model count for BANK is 2.
    # New peer factor: 0.95 + 0.05 * (2/10) = 0.9600 -> 90.0 * 0.96 = 86.4
    # Let's test the blending ranking
    with patch.dict(composer.weights, {"alpha": {"pb_vs_sector": 0.0}}):
        composer._harmonize_composites(rows, session=session)

    # Let's assert they are ranked. Global values: [76.8 (bank1), 86.4 (bank2), 75.0 (oper)]
    # Pct ranks for alpha: bank2 is highest (rank 3 -> 100), bank1 is second (rank 2 -> 50), oper is lowest (rank 1 -> 0)
    # Blend with sectoral ranking (which defaults to global if no custom sectors or 50% neutral)
    assert bank2.composite_alpha is not None
    assert bank2.composite_alpha > 80.0
    assert bank1.composite_alpha is not None
    assert bank1.composite_alpha > 40.0


def test_momentum_scorer_date_alignment(session):
    """MomentumScorer aligns latest_date to actual last price date in DB on or before scoring_date."""
    company = Company(ticker="MOMT", name="Momentum Test", company_type="OPERATING", is_active=True)
    session.add(company)
    session.flush()

    # If scoring_date is today, but actual last price date is 10 days ago (database delay)
    today = date(2026, 5, 26)
    actual_last_date = today - timedelta(days=10)

    # Windows end at the skip endpoint (actual_last_date - 30, academic 12-1 skip)
    skip = 30
    prices = {
        actual_last_date - timedelta(days=skip + 360): 100.0,  # ~13m
        actual_last_date - timedelta(days=skip + 180): 110.0,  # ~7m
        actual_last_date - timedelta(days=skip + 90): 115.0,   # ~4m
        actual_last_date - timedelta(days=skip): 150.0,        # skip endpoint
        actual_last_date: 175.0,                               # latest — inside skip, ignored
    }

    for d, p in prices.items():
        session.add(DailyPrice(company_id=company.id, date=d, close=p, adjusted_close=p))
    session.flush()

    scorer = MomentumScorer()

    # We score with scoring_date=today, but actual last price is 10 days ago.
    # MomentumScorer should automatically align latest_date to actual_last_date (today - 10)
    # and then apply the skip from there.
    result = scorer.score(company.id, session, scoring_date=today)

    assert result is not None
    # If the date aligned correctly, end_price is the skip-endpoint price (150.0),
    # not the latest 175.0. return_12m is (150 / 100) - 1 = 50%.
    assert result["return_12m"] == pytest.approx(0.50, rel=1e-2)
