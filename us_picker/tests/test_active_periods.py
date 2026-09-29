"""Survivorship-safe as-of universe via company_active_periods (2026-07-07).

Our-architecture replacement for the branch's approach: an interval table that
plugs into the EXISTING inactive_but_listed_ids / company_ids machinery. Tests
pin (a) default-safety — unseeded == legacy heuristic byte-for-byte, (b) the
seeded table produces correct point-in-time membership, (c) live dates stay
empty, (d) manual corrections survive re-seeding.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, Company, CompanyActivePeriod, DailyPrice
from us_picker.db.active_periods import (
    active_ids_from_periods,
    periods_seeded,
    seed_company_active_periods,
)
from us_picker.portfolio.universes import inactive_but_listed_ids


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _seed_companies(session):
    a = Company(ticker="AAA", name="Active", company_type="OPERATING",
                is_active=True, listing_date=date(2018, 1, 1))
    b = Company(ticker="BBB", name="DelistedLater", company_type="OPERATING",
                is_active=False, listing_date=date(2018, 1, 1),
                delisting_date=date(2022, 6, 1))
    c = Company(ticker="CCC", name="InactiveNoDelist", company_type="OPERATING",
                is_active=False, listing_date=date(2018, 1, 1))
    session.add_all([a, b, c])
    session.flush()
    # CCC has prices up to 2021-03-01 (its inferred delisting boundary).
    for d in (date(2020, 1, 1), date(2021, 3, 1)):
        session.add(DailyPrice(company_id=c.id, date=d, close=10.0))
    session.add(DailyPrice(company_id=a.id, date=date(2021, 1, 1), close=5.0))
    session.flush()
    return a, b, c


def test_unseeded_falls_back_to_heuristic(session):
    a, b, c = _seed_companies(session)
    assert periods_seeded(session) is False
    assert active_ids_from_periods(session, date(2021, 1, 1)) is None
    # Legacy heuristic: B (delisted later) + C (priced on/after) are members.
    assert inactive_but_listed_ids(session, date(2021, 1, 1)) == {b.id, c.id}
    # After both boundaries pass, nobody inactive is a member.
    assert inactive_but_listed_ids(session, date(2023, 1, 1)) == set()


def test_seed_then_membership_matches(session):
    a, b, c = _seed_companies(session)
    changed = seed_company_active_periods(session)
    session.commit()
    assert changed == 3
    assert periods_seeded(session) is True
    # Interval table now drives membership — same answer as the heuristic here,
    # proving the wiring is behavior-preserving on this data.
    assert active_ids_from_periods(session, date(2021, 1, 1)) == {b.id, c.id}
    assert inactive_but_listed_ids(session, date(2021, 1, 1)) == {b.id, c.id}


def test_seeded_live_date_is_empty(session):
    _seed_companies(session)
    seed_company_active_periods(session)
    session.commit()
    # Well after every delisting boundary -> no inactive company is active.
    assert active_ids_from_periods(session, date(2025, 1, 1)) == set()
    assert inactive_but_listed_ids(session, date(2025, 1, 1)) == set()


def test_seed_sets_expected_boundaries(session):
    a, b, c = _seed_companies(session)
    seed_company_active_periods(session)
    session.commit()
    rows = {r.company_id: r for r in session.query(CompanyActivePeriod).all()}
    assert rows[a.id].active_to is None and rows[a.id].source == "current_active"
    assert rows[b.id].active_to == date(2022, 6, 1) and rows[b.id].source == "delisting_date"
    assert rows[c.id].active_to == date(2021, 3, 1) and rows[c.id].source == "last_price_inferred"


def test_manual_correction_survives_reseed(session):
    a, b, c = _seed_companies(session)
    seed_company_active_periods(session)
    session.commit()
    # Manually correct CCC's boundary and mark it non-auto.
    row = session.query(CompanyActivePeriod).filter_by(company_id=c.id).one()
    row.active_to = date(2021, 12, 31)
    row.source = "manual"
    session.commit()
    seed_company_active_periods(session)  # re-run
    session.commit()
    row = session.query(CompanyActivePeriod).filter_by(company_id=c.id).one()
    assert row.source == "manual"
    assert row.active_to == date(2021, 12, 31)
