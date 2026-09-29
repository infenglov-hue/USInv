"""Point-in-time universe membership tests (survivorship guard).

Audit CRITICAL #4: ``Company.is_active`` is mutated in place on delisting, so
filtering on it alone silently drops every since-delisted name from
historical universes and inflates backtest results. The 2026-07-02 fix
treats ``delisting_date`` (when populated) or the last price print as the
point-in-time activity boundary. Live runs (as_of == today) must behave
exactly as before.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, Company, DailyPrice, ScoringResult
from us_picker.portfolio.universes import (
    UniverseBuilder,
    active_as_of_criterion,
    inactive_but_listed_ids,
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


def _add_company(
    session,
    ticker: str,
    is_active: bool,
    delisting_date: date | None = None,
) -> int:
    company = Company(
        ticker=ticker,
        name=ticker,
        company_type="OPERATING",
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


class TestInactiveButListedIds:
    def test_point_in_time_membership(self, session):
        _add_company(session, "ACT", is_active=True)
        delisted_after = _add_company(
            session, "DELF", is_active=False, delisting_date=date(2026, 3, 1)
        )
        _add_company(
            session, "DELP", is_active=False, delisting_date=date(2025, 1, 1)
        )
        no_delist_recent = _add_company(session, "NDR", is_active=False)
        no_delist_old = _add_company(session, "NDO", is_active=False)
        _add_price(session, no_delist_recent, date(2026, 2, 15))
        _add_price(session, no_delist_old, date(2024, 1, 10))
        session.commit()

        # As of 2026-02-01: DELF hadn't delisted yet, NDR still printed
        # prices afterwards. DELP (already delisted) and NDO (last print
        # 2024) stay out.
        ids = inactive_but_listed_ids(session, date(2026, 2, 1))
        assert ids == {delisted_after, no_delist_recent}

    def test_live_date_returns_empty_set(self, session):
        _add_company(session, "ACT", is_active=True)
        no_delist = _add_company(session, "GONE", is_active=False)
        _add_price(session, no_delist, date(2026, 2, 15))
        _add_company(
            session, "DELF", is_active=False, delisting_date=date(2026, 3, 1)
        )
        session.commit()

        # On a "today" after every delisting/last print, the extra-id set is
        # empty → live behavior identical to the plain is_active filter.
        assert inactive_but_listed_ids(session, date(2026, 7, 1)) == set()


class TestUniverseScoresPointInTime:
    def test_get_scores_includes_since_delisted_company(self, session):
        as_of = date(2026, 2, 1)
        alive = _add_company(session, "ALIVE", is_active=True)
        dead = _add_company(
            session, "DEAD", is_active=False, delisting_date=date(2026, 4, 1)
        )
        for cid in (alive, dead):
            session.add(
                ScoringResult(
                    company_id=cid,
                    scoring_date=as_of,
                    model_used="OPERATING",
                    composite_alpha=80.0,
                    data_completeness=100.0,
                )
            )
        session.commit()

        builder = UniverseBuilder(scoring_date=as_of)
        rows = builder._get_scores(session, exact_date=True)
        ids = {company_id for company_id, _score, _company in rows}

        assert ids == {alive, dead}

    def test_criterion_reduces_to_is_active_when_no_extras(self, session):
        alive = _add_company(session, "ALIVE", is_active=True)
        _add_company(session, "GONE", is_active=False)
        session.commit()

        criterion = active_as_of_criterion(session, date(2026, 7, 1))
        ids = {cid for (cid,) in session.query(Company.id).filter(criterion)}
        assert ids == {alive}
