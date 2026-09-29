import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    AdjustedMetric,
    Base,
    Company,
    CorporateAction,
    DailyPrice,
    FinancialStatement,
)
from us_picker.scoring.models.insurance import InsuranceScorer


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def _balance_json(total_assets: float, total_equity: float) -> str:
    return json.dumps(
        [
            {"item_code": "1BL", "desc_tr": "TOPLAM VARLIKLAR", "value": total_assets},
            {"item_code": "2N", "desc_tr": "OZKAYNAKLAR", "value": total_equity},
        ]
    )


def _income_json(net_sales: float) -> str:
    return json.dumps(
        [
            {"item_code": "3C", "desc_tr": "Satis Gelirleri", "value": net_sales},
        ]
    )


def test_insurance_scorer_uses_close_price_and_trailing_dividend_yield(session):
    scoring_date = date(2026, 3, 25)
    period_end = date(2025, 12, 31)

    company = Company(
        ticker="ANHYT",
        name="Anadolu Hayat",
        company_type="INSURANCE",
        sector_bist="Sigorta",
        sector_custom="Sigorta",
        is_active=True,
    )
    session.add(company)
    session.flush()

    session.add(
        DailyPrice(
            company_id=company.id,
            date=scoring_date,
            close=20.0,
            adjusted_close=None,
            high=20.5,
            low=19.5,
            volume=100_000,
            source="YAHOO_TEST",
        )
    )
    session.add(
        AdjustedMetric(
            company_id=company.id,
            period_end=period_end,
            reported_net_income=20_000_000.0,
            adjusted_net_income=20_000_000.0,
            eps_adjusted=2.0,
        )
    )
    session.add_all(
        [
            FinancialStatement(
                company_id=company.id,
                period_end=period_end,
                period_type="ANNUAL",
                statement_type="BALANCE",
                publication_date=date(2026, 3, 10),
                data_json=_balance_json(260_000_000.0, 200_000_000.0),
            ),
            FinancialStatement(
                company_id=company.id,
                period_end=period_end,
                period_type="ANNUAL",
                statement_type="INCOME",
                publication_date=date(2026, 3, 10),
                data_json=_income_json(50_000_000.0),
            ),
            CorporateAction(
                company_id=company.id,
                action_date=date(2025, 6, 1),
                action_type="DIVIDEND",
                adjustment_factor=1.2,
                source="TEST",
            ),
        ]
    )
    session.commit()

    scorer = InsuranceScorer()
    metrics = scorer._extract_metrics(company.id, session, scoring_date=scoring_date)

    assert metrics is not None
    assert metrics["pb"] == pytest.approx(1.0)
    assert metrics["roe"] == pytest.approx(0.10)
    assert metrics["net_margin"] == pytest.approx(0.40)
    assert metrics["debt_equity"] == pytest.approx(0.30)
    assert metrics["dividend_yield"] == pytest.approx(0.06)

    scores = scorer.score_all(session, scoring_date=scoring_date)
    assert company.id in scores
    assert scores[company.id]["banking_composite"] is not None
    assert scores[company.id]["data_completeness"] == pytest.approx(100.0)


def test_insurance_scorer_bist_specific_codes(session):
    scoring_date = date(2026, 3, 25)
    period_end = date(2025, 12, 31)

    company = Company(
        ticker="TURSG",
        name="Turkiye Sigorta",
        company_type="INSURANCE",
        sector_bist="Sigorta",
        sector_custom="Sigorta",
        is_active=True,
    )
    session.add(company)
    session.flush()

    session.add(
        DailyPrice(
            company_id=company.id,
            date=scoring_date,
            close=10.0,
            adjusted_close=None,
            high=10.5,
            low=9.5,
            volume=100_000,
            source="YAHOO_TEST",
        )
    )
    session.add(
        AdjustedMetric(
            company_id=company.id,
            period_end=period_end,
            reported_net_income=10_000_000.0,
            adjusted_net_income=10_000_000.0,
            eps_adjusted=1.0,
        )
    )
    
    # 1Z = Total Assets, 2O = Total Equity
    bal_data = [
        {"item_code": "1Z", "desc_tr": "AKTIF TOPLAMI", "value": 150_000_000.0},
        {"item_code": "2O", "desc_tr": "Ozsermaye Toplami", "value": 100_000_000.0},
    ]
    
    # Revenue is sum of 3A + 3D + 3FA + 3L
    inc_data = [
        {"item_code": "3A", "desc_tr": "Hayat Disi Teknik", "value": 15_000_000.0},
        {"item_code": "3D", "desc_tr": "Hayat Teknik", "value": 5_000_000.0},
        {"item_code": "3FA", "desc_tr": "Emeklilik Teknik", "value": 2_000_000.0},
        {"item_code": "3L", "desc_tr": "Yatirim Gelirleri", "value": 3_000_000.0},
    ]
    
    session.add_all(
        [
            FinancialStatement(
                company_id=company.id,
                period_end=period_end,
                period_type="ANNUAL",
                statement_type="BALANCE",
                publication_date=date(2026, 3, 10),
                data_json=json.dumps(bal_data),
            ),
            FinancialStatement(
                company_id=company.id,
                period_end=period_end,
                period_type="ANNUAL",
                statement_type="INCOME",
                publication_date=date(2026, 3, 10),
                data_json=json.dumps(inc_data),
            ),
        ]
    )
    session.commit()

    scorer = InsuranceScorer()
    metrics = scorer._extract_metrics(company.id, session, scoring_date=scoring_date)

    assert metrics is not None
    # shares = 10_000_000 / 1.0 = 10_000_000
    # pb = (10.0 * 10_000_000) / 100_000_000 = 1.0
    assert metrics["pb"] == pytest.approx(1.0)
    # roe = 10_000_000 / 100_000_000 = 0.10
    assert metrics["roe"] == pytest.approx(0.10)
    # total revenue = 15m + 5m + 2m + 3m = 25_000_000
    # net_margin = 10_000_000 / 25_000_000 = 0.40
    assert metrics["net_margin"] == pytest.approx(0.40)
    # debt_equity = (150m - 100m) / 100m = 0.50
    assert metrics["debt_equity"] == pytest.approx(0.50)

