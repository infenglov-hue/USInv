"""Split-normalized units and open-position restatement (US port)."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    CorporateAction,
    DailyPrice,
    PortfolioCycleMark,
    PortfolioSelection,
)
from us_picker.portfolio.split_positions import apply_splits_to_open_positions
from us_picker.utils.splits import (
    cumulative_split_factor,
    invalidate_split_cache,
    split_factor_between,
    to_base_shares,
    valuation_price,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    invalidate_split_cache()
    s = sessionmaker(bind=engine)()
    company = Company(ticker="NVDA", is_active=True)
    s.add(company)
    s.flush()
    s.add(
        CorporateAction(
            company_id=company.id,
            action_date=date(2024, 6, 10),
            action_type="SPLIT",
            adjustment_factor=10.0,
        )
    )
    s.add_all(
        [
            DailyPrice(company_id=company.id, date=date(2024, 6, 7), close=1208.88),
            DailyPrice(company_id=company.id, date=date(2024, 6, 10), close=121.79),
        ]
    )
    s.commit()
    yield s
    s.close()
    invalidate_split_cache()


def test_cumulative_factor_only_counts_splits_known_by_the_date(session):
    cid = session.query(Company.id).scalar()
    assert cumulative_split_factor(session, cid, date(2024, 6, 7)) == 1.0
    assert cumulative_split_factor(session, cid, date(2024, 6, 10)) == 10.0
    assert split_factor_between(session, cid, date(2024, 4, 28), date(2024, 7, 1)) == 10.0


def test_market_cap_is_continuous_across_a_split(session):
    cid = session.query(Company.id).scalar()
    # 10-Q for the April quarter reports 2.46B pre-split shares.
    base_shares = to_base_shares(session, cid, 2.46e9, date(2024, 4, 28))
    before = valuation_price(session, cid, date(2024, 6, 7)) * base_shares
    after = valuation_price(session, cid, date(2024, 6, 10)) * base_shares
    assert before == pytest.approx(1208.88 * 2.46e9)
    assert after == pytest.approx(121.79 * 10 * 2.46e9)
    assert after / before == pytest.approx(121.79 * 10 / 1208.88)


def test_cover_count_already_post_split_is_not_double_counted(session):
    cid = session.query(Company.id).scalar()
    pre = to_base_shares(session, cid, 2.46e9, date(2024, 4, 28))
    post = to_base_shares(session, cid, 24.6e9, date(2024, 8, 20))
    assert pre == pytest.approx(post)


def test_open_position_is_restated_once(session):
    cid = session.query(Company.id).scalar()
    position = PortfolioSelection(
        portfolio="ALPHA",
        selection_date=date(2024, 5, 27),
        company_id=cid,
        entry_price=1100.0,
        stop_loss_price=900.0,
        target_price=1500.0,
        highest_close=1210.0,
    )
    session.add(position)
    session.add(
        PortfolioCycleMark(
            portfolio="ALPHA", cycle_date=date(2024, 5, 27), company_id=cid, ref_price=1100.0
        )
    )
    session.commit()

    events = apply_splits_to_open_positions(session, today=date(2024, 6, 10))
    assert [e["ratio"] for e in events] == [10.0]
    assert position.entry_price == pytest.approx(110.0)
    assert position.stop_loss_price == pytest.approx(90.0)
    assert position.highest_close == pytest.approx(121.0)
    mark = session.query(PortfolioCycleMark).one()
    assert mark.ref_price == pytest.approx(110.0)

    assert apply_splits_to_open_positions(session, today=date(2024, 6, 20)) == []
    assert position.entry_price == pytest.approx(110.0)
