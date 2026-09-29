"""Corporate-action price adjustment (2026-07-09).

Pins the band-violation detector + back-adjuster that fixes the
"split reads as a crash" defect (BIMAS 2026-05-14: momentum 91 -> 8.7 on a
~2:1 bonus issue; adjusted_close arrived == close on the whole table).
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.cleaning.price_adjust import (
    AdjustConfig,
    adjusted_series,
    detect_events,
    rebuild_adjusted_closes,
)
from us_picker.db.schema import Base, Company, DailyPrice

CFG = AdjustConfig(
    enabled=True, low_ratio=0.75, high_ratio=1.30, max_gap_days=7, ipo_grace_days=30
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _days(start: date, closes: list[float], step: int = 1):
    return [(start + timedelta(days=i * step), c) for i, c in enumerate(closes)]


# ── detection ────────────────────────────────────────────────────────────────

def test_detects_two_for_one_split():
    rows = _days(date(2026, 5, 10), [100.0, 102.0, 51.0, 52.0])
    events = detect_events(rows, listing_date=date(2020, 1, 1), config=CFG)
    assert len(events) == 1
    event_date, ratio = events[0]
    assert event_date == date(2026, 5, 12)
    assert ratio == pytest.approx(0.5, abs=0.01)


def test_band_moves_not_flagged():
    # ±10% daily band moves are normal — never events.
    rows = _days(date(2026, 5, 10), [100.0, 110.0, 99.0, 89.5, 98.0])
    assert detect_events(rows, date(2020, 1, 1), CFG) == []


def test_suspension_gap_not_flagged():
    # A -60% print after a 30-day halt is a real repricing, not an action.
    rows = [
        (date(2026, 1, 5), 100.0),
        (date(2026, 1, 6), 101.0),
        (date(2026, 2, 10), 40.0),  # 35 days later
    ]
    assert detect_events(rows, date(2020, 1, 1), CFG) == []


def test_ipo_grace_window_skipped():
    listing = date(2026, 5, 1)
    rows = _days(date(2026, 5, 2), [10.0, 15.0, 22.0])  # discovery pops
    assert detect_events(rows, listing, CFG) == []
    # Same moves well after listing WOULD be flagged.
    rows_late = _days(date(2026, 7, 1), [10.0, 15.0, 22.0])
    assert len(detect_events(rows_late, listing, CFG)) == 2


# ── adjustment math ─────────────────────────────────────────────────────────

def test_back_adjustment_restores_continuity():
    rows = _days(date(2026, 5, 10), [100.0, 102.0, 51.0, 52.0])
    events = detect_events(rows, date(2020, 1, 1), CFG)
    adj = adjusted_series(rows, events)
    # Pre-split closes are halved; post-split untouched.
    assert adj[0] == pytest.approx(50.0)
    assert adj[1] == pytest.approx(51.0)
    assert adj[2] == pytest.approx(51.0)
    assert adj[3] == pytest.approx(52.0)
    # No fake -50% move remains.
    returns = [adj[i] / adj[i - 1] - 1 for i in range(1, len(adj))]
    assert all(abs(r) < 0.11 for r in returns)


def test_multiple_events_compound():
    # 1:2 split, then later a 1:2 again -> oldest rows scaled by 0.25.
    rows = _days(date(2026, 1, 1), [100.0, 50.0, 50.0, 25.0, 25.0])
    events = detect_events(rows, date(2020, 1, 1), CFG)
    assert len(events) == 2
    adj = adjusted_series(rows, events)
    assert adj[0] == pytest.approx(25.0)
    assert adj[-1] == pytest.approx(25.0)


# ── DB rebuild ──────────────────────────────────────────────────────────────

def _seed(session, ticker, closes, ctype="OPERATING", listing=date(2020, 1, 1)):
    company = Company(
        ticker=ticker, name=ticker, company_type=ctype, is_active=True,
        listing_date=listing,
    )
    session.add(company)
    session.flush()
    for i, c in enumerate(closes):
        session.add(
            DailyPrice(
                company_id=company.id,
                date=date(2026, 5, 10) + timedelta(days=i),
                close=c,
                adjusted_close=c,  # feed delivers adjusted == close
            )
        )
    session.flush()
    return company


def test_rebuild_writes_only_changed_rows_and_is_idempotent(session):
    company = _seed(session, "SPLT", [100.0, 102.0, 51.0, 52.0])
    stats = rebuild_adjusted_closes(session, config=CFG)
    assert stats["companies_with_events"] == 1
    assert stats["events"] == 1
    assert stats["rows_updated"] == 2  # only the two pre-split rows change

    adj = [
        r.adjusted_close
        for r in session.query(DailyPrice)
        .filter(DailyPrice.company_id == company.id)
        .order_by(DailyPrice.date)
    ]
    assert adj == pytest.approx([50.0, 51.0, 51.0, 52.0])
    # close column untouched
    raw = [
        r.close
        for r in session.query(DailyPrice)
        .filter(DailyPrice.company_id == company.id)
        .order_by(DailyPrice.date)
    ]
    assert raw == pytest.approx([100.0, 102.0, 51.0, 52.0])

    # Second run: nothing to do.
    stats2 = rebuild_adjusted_closes(session, config=CFG)
    assert stats2["rows_updated"] == 0


def test_rebuild_skips_index_companies(session):
    _seed(session, "SPY", [10000.0, 4000.0, 4100.0], ctype="INDEX")
    stats = rebuild_adjusted_closes(session, config=CFG)
    assert stats["companies_with_events"] == 0


def test_rebuild_no_events_no_writes(session):
    _seed(session, "CALM", [100.0, 103.0, 101.0])
    stats = rebuild_adjusted_closes(session, config=CFG)
    assert stats["companies_with_events"] == 0
    assert stats["rows_updated"] == 0
