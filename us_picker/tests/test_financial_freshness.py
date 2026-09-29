"""Financial freshness monitoring + current-year quarterly fetch (2026-07-09).

Pins the fixes for the Q1-2026 staleness incident:
  (a) `expected_latest_period_end` mirrors the scoring PIT calendar, so the
      freshness alarm and the scorers can never disagree about which period
      should be present;
  (b) `check_financial_freshness` flags a DB whose newest broad period lags
      the filing calendar (the incident state: 2025-12-31 on 2026-07-09);
  (c) `fetch_recent_financials` requests `start_year = current_year + 1` —
      IsYatirim's MaliTablo template is anchored at start_year - 1, so this
      is the only way the year-in-progress interim quarters (2026/3 while in
      2026) are requested at all — and all-null older periods are skipped,
      never overwriting existing history.
"""

from datetime import date

import pandas as pd
import pytest
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.data.fetcher import DataFetcher
from us_picker.data.freshness import (
    check_financial_freshness,
    check_score_input_freshness,
    expected_latest_period_end,
    visibility_lag_days,
)
from us_picker.db.schema import AdjustedMetric, Base, Company, FinancialStatement


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


# ── expected period per the SPK calendar ────────────────────────────────────

@pytest.mark.parametrize(
    ("today", "expected"),
    [
        # Q1 visible from 03-31 + 46 + 5 grace = 05-21.
        (date(2026, 5, 20), date(2025, 12, 31)),
        (date(2026, 5, 21), date(2026, 3, 31)),
        # The incident date: Q1-2026 long expected.
        (date(2026, 7, 9), date(2026, 3, 31)),
        # H1 visible from 06-30 + 56 + 5 = 08-30.
        (date(2026, 8, 29), date(2026, 3, 31)),
        (date(2026, 8, 30), date(2026, 6, 30)),
        # Annual visible from 12-31 + 76 + 5 = ~03-22 next year.
        (date(2026, 3, 21), date(2025, 9, 30)),
        (date(2026, 3, 22), date(2025, 12, 31)),
    ],
)
def test_expected_latest_period_end(today, expected):
    assert expected_latest_period_end(today) == expected


def test_visibility_lags_mirror_scoring_pit():
    # Q1/Q3: 46d, H1: 56d, annual: 76d — must match scoring/context.py.
    assert visibility_lag_days(date(2026, 3, 31)) == 46
    assert visibility_lag_days(date(2026, 9, 30)) == 46
    assert visibility_lag_days(date(2026, 6, 30)) == 56
    assert visibility_lag_days(date(2026, 12, 31)) == 76


# ── DB freshness check ──────────────────────────────────────────────────────

def _seed_statements(session, period_end, n_companies, start_idx=0):
    for i in range(n_companies):
        ticker = f"T{start_idx + i:03d}"
        company = session.query(Company).filter_by(ticker=ticker).first()
        if company is None:
            company = Company(
                ticker=ticker, name=ticker, company_type="OPERATING", is_active=True
            )
            session.add(company)
            session.flush()
        session.add(
            FinancialStatement(
                company_id=company.id,
                period_end=period_end,
                period_type="Q1" if period_end.month == 3 else "ANNUAL",
                statement_type="INCOME",
                version=1,
                data_json="[]",
            )
        )
    session.flush()


def test_check_freshness_stale_when_quarter_missing(session):
    # The incident: broad Q4-2025 coverage, zero Q1-2026, checked in July.
    _seed_statements(session, date(2025, 12, 31), n_companies=5)
    report = check_financial_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )
    assert report["fresh"] is False
    assert report["expected_period_end"] == "2026-03-31"
    assert report["actual_period_end"] == "2025-12-31"


def test_check_freshness_ok_when_quarter_present(session):
    _seed_statements(session, date(2025, 12, 31), n_companies=5)
    _seed_statements(session, date(2026, 3, 31), n_companies=4)
    report = check_financial_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )
    assert report["fresh"] is True
    assert report["actual_period_end"] == "2026-03-31"


def test_check_freshness_early_filers_below_threshold_do_not_mask(session):
    # 2 early filers must not count as universe-wide arrival.
    _seed_statements(session, date(2025, 12, 31), n_companies=5)
    _seed_statements(session, date(2026, 3, 31), n_companies=2, start_idx=100)
    report = check_financial_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )
    assert report["fresh"] is False
    assert report["actual_period_end"] == "2025-12-31"


def test_check_freshness_empty_db_is_stale(session):
    report = check_financial_freshness(session, today=date(2026, 7, 9))
    assert report["fresh"] is False
    assert report["actual_period_end"] is None


def _seed_score_inputs(session, period_end, n_companies, start_idx=0):
    for i in range(n_companies):
        ticker = f"S{start_idx + i:03d}"
        company = session.query(Company).filter_by(ticker=ticker).first()
        if company is None:
            company = Company(
                ticker=ticker, name=ticker, company_type="OPERATING", is_active=True
            )
            session.add(company)
            session.flush()
        session.add(
            AdjustedMetric(
                company_id=company.id,
                period_end=period_end,
                source_period_type=("Q1" if period_end.month == 3 else "ANNUAL"),
                calculation_basis=("TTM" if period_end.month != 12 else "ANNUAL"),
                input_hash=f"{company.id:064x}"[-64:],
            )
        )
    session.flush()


def test_score_input_freshness_rejects_raw_only_false_green(session):
    _seed_statements(session, date(2026, 3, 31), n_companies=4)
    _seed_score_inputs(session, date(2025, 12, 31), n_companies=4)

    raw = check_financial_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )
    score_inputs = check_score_input_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )

    assert raw["fresh"] is True
    assert score_inputs["fresh"] is False
    assert score_inputs["actual_period_end"] == "2025-12-31"


def test_score_input_freshness_accepts_audited_q1_ttm(session):
    _seed_score_inputs(session, date(2026, 3, 31), n_companies=4)
    report = check_score_input_freshness(
        session, today=date(2026, 7, 9), min_companies=3
    )

    assert report["fresh"] is True
    assert report["layer"] == "score_inputs"
    assert report["actual_period_end"] == "2026-03-31"


# ── fetch_recent_financials wiring ──────────────────────────────────────────

class _FakeSEC:
    """EDGAR stub: one recent 10-Q filer and a tiny companyfacts payload."""

    def __init__(self, filers):
        self.filers = filers
        self.facts_calls = []

    def recent_periodic_filers(self, since, until=None):
        return set(self.filers)

    def fetch_companyfacts(self, cik):
        self.facts_calls.append(cik)
        q1 = {"start": "2026-01-01", "end": "2026-03-31", "val": 1234.5,
              "filed": "2026-05-01", "form": "10-Q", "accn": "x"}
        return {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [q1]}},
            "NetIncomeLoss": {"units": {"USD": [dict(q1, val=100.0)]}},
        }}}


def _bare_fetcher(session, filers):
    return DataFetcher(session=session, console=Console(quiet=True), sec_client=_FakeSEC(filers))


def test_fetch_recent_financials_only_refreshes_recent_filers(session):
    session.add_all([
        Company(ticker="FILED", cik=111, company_type="OPERATING", is_active=True),
        Company(ticker="QUIET", cik=222, company_type="OPERATING", is_active=True),
    ])
    session.flush()

    fetcher = _bare_fetcher(session, filers={111, 999})
    stats = fetcher.fetch_recent_financials()

    assert fetcher.sec.facts_calls == [111]
    assert stats["tickers_processed"] == 1
    rows = session.query(FinancialStatement).all()
    assert len(rows) == 1
    assert rows[0].period_end == date(2026, 3, 31)
    assert rows[0].period_type == "Q1"
    assert rows[0].publication_date == date(2026, 5, 2)


def test_fetch_recent_financials_no_universe_filers_is_a_noop(session):
    fetcher = _bare_fetcher(session, filers={999})
    stats = fetcher.fetch_recent_financials()
    assert stats["tickers_processed"] == 0
    assert session.query(FinancialStatement).count() == 0
