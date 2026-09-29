"""Tests for the AdjustedMetric point-in-time guard (audit CRITICAL #1).

Verifies the centralized ``_adjusted_metric_pit_filter`` SQLAlchemy filter:
  * Rows with ``publication_date <= scoring_date`` are visible.
  * Rows with ``publication_date > scoring_date`` are filtered out.
  * Legacy rows (``publication_date IS NULL``) fall back to the 76-day
    period_end heuristic so years of historical data don't disappear.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import AdjustedMetric, Base, Company
from us_picker.scoring.context import (
    _adjusted_metric_pit_filter,
    _ESTIMATED_LAG_H1_DAYS,
    _ESTIMATED_LAG_QUARTER_DAYS,
    _LEGACY_PUBLICATION_LAG_DAYS,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


def _add_company(session, ticker: str = "TEST") -> int:
    c = Company(ticker=ticker, name=f"{ticker} A.S.",
                company_type="OPERATING", is_active=True)
    session.add(c); session.flush()
    return c.id


def _add_metric(
    session,
    cid: int,
    period_end: date,
    publication_date: date | None = None,
):
    """Add a minimal AdjustedMetric row with the given dates."""
    m = AdjustedMetric(
        company_id=cid,
        period_end=period_end,
        publication_date=publication_date,
        adjusted_net_income=1.0,
        eps_adjusted=1.0,
    )
    session.add(m); session.flush()
    return m


class TestAdjustedMetricPitFilter:
    """Confirm the filter ships rows that were knowable on the scoring date."""

    def test_filed_before_scoring_date_is_visible(self, session):
        """Row with publication_date <= scoring_date passes the filter."""
        cid = _add_company(session)
        _add_metric(
            session, cid,
            period_end=date(2024, 12, 31),
            publication_date=date(2025, 3, 15),  # filed on time
        )
        session.commit()

        scoring_date = date(2025, 4, 1)
        rows = (
            session.query(AdjustedMetric)
            .filter(_adjusted_metric_pit_filter(scoring_date))
            .all()
        )
        assert len(rows) == 1, "Filed row must be visible after publication_date"

    def test_filed_after_scoring_date_is_hidden(self, session):
        """Row with publication_date > scoring_date is invisible (no leak)."""
        cid = _add_company(session)
        _add_metric(
            session, cid,
            period_end=date(2024, 12, 31),
            publication_date=date(2025, 5, 20),  # late filer
        )
        session.commit()

        # Scoring "as of" April 1 — the late Q4-2024 filing isn't out yet.
        scoring_date = date(2025, 4, 1)
        rows = (
            session.query(AdjustedMetric)
            .filter(_adjusted_metric_pit_filter(scoring_date))
            .all()
        )
        assert rows == [], (
            "Row filed AFTER scoring_date must not leak into the past."
        )

    def test_legacy_row_uses_76_day_heuristic(self, session):
        """publication_date IS NULL → falls back to period_end + 76 days."""
        cid = _add_company(session)
        scoring_date = date(2025, 4, 1)
        # period_end well over 76 days before scoring → visible by heuristic.
        _add_metric(
            session, cid,
            period_end=scoring_date - timedelta(days=120),
            publication_date=None,
        )
        # period_end too recent → filter says "could not have been knowable".
        _add_metric(
            session, cid,
            period_end=scoring_date - timedelta(days=30),
            publication_date=None,
        )
        session.commit()

        rows = (
            session.query(AdjustedMetric)
            .filter(_adjusted_metric_pit_filter(scoring_date))
            .order_by(AdjustedMetric.period_end)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].period_end == scoring_date - timedelta(days=120)

    def test_mixed_legacy_and_new_rows_are_combined(self, session):
        """Both code paths coexist on the same query.

        Three different companies × one annual period each, so the
        ``(company_id, period_end)`` unique constraint isn't tripped.
        """
        scoring_date = date(2025, 4, 1)

        # Company A: legacy row (no pub date), old period — passes heuristic.
        cid_a = _add_company(session, "AAA")
        _add_metric(
            session, cid_a,
            period_end=date(2023, 12, 31),
            publication_date=None,
        )
        # Company B: new row, on-time filing — passes strict path.
        cid_b = _add_company(session, "BBB")
        _add_metric(
            session, cid_b,
            period_end=date(2024, 12, 31),
            publication_date=date(2025, 3, 10),
        )
        # Company C: new row, late filing (after scoring_date) — must be hidden.
        cid_c = _add_company(session, "CCC")
        _add_metric(
            session, cid_c,
            period_end=date(2024, 12, 31),
            publication_date=date(2025, 5, 5),
        )
        session.commit()

        rows = (
            session.query(AdjustedMetric)
            .filter(_adjusted_metric_pit_filter(scoring_date))
            .order_by(AdjustedMetric.period_end)
            .all()
        )
        # 2 of 3 should pass: AAA (legacy heuristic) and BBB (strict path).
        # CCC is filed AFTER scoring_date so it must not leak.
        assert len(rows) == 2
        cids = sorted(r.company_id for r in rows)
        assert cids == sorted([cid_a, cid_b])

    def test_legacy_lag_constant_is_76_days(self):
        """Document the heuristic lags for future maintainers.

        SPK Seri II-14.1 deadlines (consolidated) + safety buffer:
        Q1/Q3 40d→46, H1 50d→56, annual 70d→76.
        """
        assert _LEGACY_PUBLICATION_LAG_DAYS == 76
        assert _ESTIMATED_LAG_QUARTER_DAYS == 46
        assert _ESTIMATED_LAG_H1_DAYS == 56

    def _visible_count(self, session, scoring_date):
        return (
            session.query(AdjustedMetric)
            .filter(_adjusted_metric_pit_filter(scoring_date))
            .count()
        )

    def test_q1_period_uses_quarter_deadline(self, session):
        """NULL-pub Q1 row becomes visible after 46 days, not 76."""
        cid = _add_company(session)
        _add_metric(session, cid, period_end=date(2025, 3, 31), publication_date=None)
        session.commit()

        # 40 days after period end: SPK deadline not passed -> hidden
        assert self._visible_count(session, date(2025, 5, 10)) == 0
        # 47 days after: deadline + buffer passed -> visible (old flat
        # heuristic would have hidden this until mid-June)
        assert self._visible_count(session, date(2025, 5, 17)) == 1

    def test_h1_period_uses_half_year_deadline(self, session):
        """NULL-pub H1 row becomes visible after 56 days."""
        cid = _add_company(session)
        _add_metric(session, cid, period_end=date(2024, 6, 30), publication_date=None)
        session.commit()

        assert self._visible_count(session, date(2024, 8, 20)) == 0   # +51d
        assert self._visible_count(session, date(2024, 8, 27)) == 1   # +58d

    def test_annual_period_keeps_76_day_lag(self, session):
        """NULL-pub annual (December) row keeps the conservative 76-day lag."""
        cid = _add_company(session)
        _add_metric(session, cid, period_end=date(2024, 12, 31), publication_date=None)
        session.commit()

        assert self._visible_count(session, date(2025, 3, 10)) == 0   # +69d
        assert self._visible_count(session, date(2025, 3, 20)) == 1   # +79d

    def test_real_publication_date_beats_estimate(self, session):
        """A real publication_date overrides the deadline estimate entirely."""
        cid = _add_company(session)
        # Q1 filed unusually early (15 days after period end)
        _add_metric(
            session, cid,
            period_end=date(2025, 3, 31),
            publication_date=date(2025, 4, 15),
        )
        session.commit()

        # Visible from the real filing date, well before the 46-day estimate
        assert self._visible_count(session, date(2025, 4, 16)) == 1
        # And hidden before the real filing date
        assert self._visible_count(session, date(2025, 4, 10)) == 0
