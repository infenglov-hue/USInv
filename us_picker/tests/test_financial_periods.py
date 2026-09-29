"""Quarterly-to-TTM financial score-input regression tests."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.cleaning.financial_periods import (
    analytical_statement_from_map,
    compose_ttm_items,
    latest_statement_map,
)
from us_picker.cleaning.financial_prep import MetricsCalculator
from us_picker.db.schema import (
    AdjustedMetric,
    Base,
    Company,
    FinancialStatement,
    ScoringResult,
)
from us_picker.scoring.version import SCORING_PIPELINE_VERSION
from us_picker.scoring.context import ScoringContext


def _items(**values):
    return [
        {
            "item_code": code,
            "desc_tr": code,
            "desc_eng": code,
            "value": value,
        }
        for code, value in values.items()
    ]


def test_compose_ttm_items_uses_annual_plus_current_ytd_minus_prior_ytd():
    current = _items(**{"3C": 30.0, "3Z": 12.0, "NEW": 4.0})
    annual = _items(**{"3C": 100.0, "3Z": 40.0, "NEW": 8.0})
    prior_ytd = _items(**{"3C": 20.0, "3Z": 10.0})

    result = {item["item_code"]: item["value"] for item in compose_ttm_items(
        current, annual, prior_ytd
    )}

    assert result["3C"] == 110.0
    assert result["3Z"] == 42.0
    # Missing is unknown, never silently zero-filled.
    assert result["NEW"] is None


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _statement(
    company_id: int,
    period_end: date,
    statement_type: str,
    values: dict[str, float],
    publication_date: date | None = None,
) -> FinancialStatement:
    period_types = {3: "Q1", 6: "Q2", 9: "Q3", 12: "ANNUAL"}
    return FinancialStatement(
        company_id=company_id,
        period_end=period_end,
        period_type=period_types[period_end.month],
        statement_type=statement_type,
        publication_date=publication_date,
        version=1,
        data_json=json.dumps(_items(**values)),
    )


def test_analytical_statement_balance_is_point_in_time_and_income_is_ttm(session):
    company = Company(ticker="TTM1", name="TTM1", company_type="OPERATING")
    session.add(company)
    session.flush()
    rows = [
        _statement(company.id, date(2025, 12, 31), "INCOME", {"3Z": 100.0}),
        _statement(company.id, date(2025, 3, 31), "INCOME", {"3Z": 20.0}),
        _statement(
            company.id,
            date(2026, 3, 31),
            "INCOME",
            {"3Z": 30.0},
            publication_date=date(2026, 5, 8),
        ),
        _statement(company.id, date(2026, 3, 31), "BALANCE", {"2N": 200.0}),
    ]
    session.add_all(rows)
    session.flush()
    mapping = latest_statement_map(rows)

    income = analytical_statement_from_map(
        mapping, company.id, date(2026, 3, 31), "INCOME"
    )
    balance = analytical_statement_from_map(
        mapping, company.id, date(2026, 3, 31), "BALANCE"
    )

    assert income is not None
    assert income.calculation_basis == "TTM"
    assert json.loads(income.data_json)[0]["value"] == 110.0
    assert income.publication_date == date(2026, 5, 8)
    assert balance is not None
    assert balance.calculation_basis == "POINT_IN_TIME"
    assert json.loads(balance.data_json)[0]["value"] == 200.0


def _seed_period(
    session,
    company_id: int,
    period_end: date,
    *,
    net_income: float,
    sales: float,
    equity: float,
    assets: float,
    capital: float,
    cfo: float,
    capex: float,
    publication_date: date | None = None,
):
    session.add_all(
        [
            _statement(
                company_id,
                period_end,
                "INCOME",
                {"3Z": net_income, "3C": sales, "4B": 5.0},
                publication_date,
            ),
            _statement(
                company_id,
                period_end,
                "BALANCE",
                {"2N": equity, "1BL": assets, "2OA": capital, "1BC": 50.0},
                publication_date,
            ),
            _statement(
                company_id,
                period_end,
                "CASHFLOW",
                {"4C": cfo, "4CAI": capex, "4CAB": 5.0, "4CAF": 0.0},
                publication_date,
            ),
        ]
    )


def test_metrics_calculator_materializes_q1_ttm_with_yoy_denominators(session):
    company = Company(
        ticker="TTM2", name="TTM2", company_type="OPERATING", is_active=True
    )
    session.add(company)
    session.flush()

    # Q1-2025 is itself TTM from 2024 annual + Q1-2025 - Q1-2024.
    _seed_period(
        session,
        company.id,
        date(2024, 3, 31),
        net_income=10,
        sales=40,
        equity=120,
        assets=240,
        capital=100,
        cfo=12,
        capex=-3,
    )
    _seed_period(
        session,
        company.id,
        date(2024, 12, 31),
        net_income=80,
        sales=300,
        equity=150,
        assets=300,
        capital=100,
        cfo=90,
        capex=-20,
    )
    _seed_period(
        session,
        company.id,
        date(2025, 3, 31),
        net_income=20,
        sales=70,
        equity=160,
        assets=320,
        capital=100,
        cfo=25,
        capex=-5,
    )
    _seed_period(
        session,
        company.id,
        date(2025, 12, 31),
        net_income=100,
        sales=400,
        equity=180,
        assets=360,
        capital=100,
        cfo=120,
        capex=-30,
    )
    _seed_period(
        session,
        company.id,
        date(2026, 3, 31),
        net_income=30,
        sales=90,
        equity=200,
        assets=400,
        capital=100,
        cfo=35,
        capex=-8,
        publication_date=date(2026, 5, 10),
    )
    session.flush()

    count = MetricsCalculator(session).calculate_adjusted_metrics(company.id)
    metric = session.query(AdjustedMetric).filter_by(
        company_id=company.id, period_end=date(2026, 3, 31)
    ).one()

    assert count == 4  # Q1-2024 lacks prior sources and is intentionally skipped.
    assert metric.source_period_type == "Q1"
    assert metric.calculation_basis == "TTM"
    assert metric.reported_net_income == pytest.approx(110.0)
    assert metric.adjusted_net_income == pytest.approx(110.0)
    assert metric.eps_adjusted == pytest.approx(1.1)
    # Same-quarter YoY balance denominator: avg(200, 160), not Dec-2025 180.
    assert metric.roe_adjusted == pytest.approx(110.0 / 180.0)
    assert metric.roa_adjusted == pytest.approx(110.0 / 360.0)
    assert metric.free_cash_flow == pytest.approx(130.0 - 33.0)
    assert metric.publication_date == date(2026, 5, 10)
    assert len(metric.input_hash or "") == 64
    manifest = json.loads(metric.source_periods_json)
    assert [row["period_end"] for row in manifest["INCOME"]] == [
        "2026-03-31",
        "2025-12-31",
        "2025-03-31",
    ]

    before_filing = ScoringResult(
        company_id=company.id,
        scoring_date=date(2026, 5, 9),
        pipeline_version=SCORING_PIPELINE_VERSION,
    )
    on_filing = ScoringResult(
        company_id=company.id,
        scoring_date=date(2026, 5, 10),
        pipeline_version=SCORING_PIPELINE_VERSION,
    )
    session.add_all([before_filing, on_filing])
    session.flush()

    q1_income = session.query(FinancialStatement).filter_by(
        company_id=company.id,
        period_end=date(2026, 3, 31),
        statement_type="INCOME",
    ).one()
    items = json.loads(q1_income.data_json)
    next(item for item in items if item["item_code"] == "3Z")["value"] = 31.0
    q1_income.data_json = json.dumps(items)
    session.flush()

    MetricsCalculator(session).calculate_adjusted_metrics(company.id)
    session.refresh(before_filing)
    session.refresh(on_filing)
    assert before_filing.pipeline_version == SCORING_PIPELINE_VERSION
    assert on_filing.pipeline_version is None

    # An idempotent clean with the same input hash must not invalidate again.
    on_filing.pipeline_version = SCORING_PIPELINE_VERSION
    session.flush()
    MetricsCalculator(session).calculate_adjusted_metrics(company.id)
    session.refresh(on_filing)
    assert on_filing.pipeline_version == SCORING_PIPELINE_VERSION


def test_metrics_calculator_skips_incomplete_interim_ttm(session):
    company = Company(
        ticker="TTM3", name="TTM3", company_type="OPERATING", is_active=True
    )
    session.add(company)
    session.flush()
    _seed_period(
        session,
        company.id,
        date(2025, 12, 31),
        net_income=100,
        sales=400,
        equity=180,
        assets=360,
        capital=100,
        cfo=120,
        capex=-30,
    )
    # No Q1-2025 comparative: Q1-2026 must not be annualized from three months.
    _seed_period(
        session,
        company.id,
        date(2026, 3, 31),
        net_income=30,
        sales=90,
        equity=200,
        assets=400,
        capital=100,
        cfo=35,
        capex=-8,
    )
    session.flush()

    MetricsCalculator(session).calculate_adjusted_metrics(company.id)

    assert session.query(AdjustedMetric).filter_by(
        company_id=company.id, period_end=date(2026, 3, 31)
    ).count() == 0


def test_scoring_context_exposes_annual_history_latest_ttm_and_yoy_pair(session):
    company = Company(
        ticker="TTM4", name="TTM4", company_type="OPERATING", is_active=True
    )
    session.add(company)
    session.flush()

    rows = [
        _statement(company.id, date(2024, 12, 31), "INCOME", {"3Z": 80.0}),
        _statement(company.id, date(2025, 3, 31), "INCOME", {"3Z": 20.0}),
        _statement(company.id, date(2025, 12, 31), "INCOME", {"3Z": 100.0}),
        _statement(company.id, date(2026, 3, 31), "INCOME", {"3Z": 30.0}),
    ]
    session.add_all(rows)
    session.add_all(
        [
            AdjustedMetric(
                company_id=company.id,
                period_end=date(2024, 12, 31),
                source_period_type="ANNUAL",
                calculation_basis="ANNUAL",
            ),
            AdjustedMetric(
                company_id=company.id,
                period_end=date(2025, 3, 31),
                source_period_type="Q1",
                calculation_basis="TTM",
            ),
            AdjustedMetric(
                company_id=company.id,
                period_end=date(2025, 12, 31),
                source_period_type="ANNUAL",
                calculation_basis="ANNUAL",
            ),
            AdjustedMetric(
                company_id=company.id,
                period_end=date(2026, 3, 31),
                source_period_type="Q1",
                calculation_basis="TTM",
            ),
        ]
    )
    session.flush()

    context = ScoringContext(session, scoring_date=date(2026, 7, 10))
    context.load_data([company.id])

    assert [metric.period_end for metric in context.get_metrics(company.id)] == [
        date(2024, 12, 31),
        date(2025, 12, 31),
        date(2026, 3, 31),
    ]
    current, comparable = context.get_latest_and_yoy_metrics(company.id)
    assert current.period_end == date(2026, 3, 31)
    assert comparable.period_end == date(2025, 3, 31)

    statements = context.get_statements(company.id, "INCOME")
    assert [statement.period_end for statement in statements] == [
        date(2024, 12, 31),
        date(2025, 12, 31),
        date(2026, 3, 31),
    ]
    latest_items = json.loads(statements[-1].data_json)
    assert latest_items[0]["value"] == 110.0
    assert statements[-1].calculation_basis == "TTM"
