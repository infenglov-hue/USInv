"""Split-normalized units and open-position restatement (US port)."""

from datetime import date
from types import SimpleNamespace

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
from us_picker.portfolio.exit_rules import ExitRuleChecker
from us_picker.portfolio.selector import PortfolioSelector
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


def test_split_restatement_waits_for_post_split_price(session):
    cid = session.query(Company.id).scalar()
    split_date = date(2024, 6, 10)
    session.query(DailyPrice).filter(DailyPrice.date == split_date).delete()
    position = PortfolioSelection(
        portfolio="ALPHA", company_id=cid, selection_date=date(2024, 6, 7),
        entry_price=1200.0, stop_loss_price=900.0,
    )
    session.add(position)
    session.flush()

    assert apply_splits_to_open_positions(session, today=split_date) == []
    assert position.entry_price == 1200.0
    assert position.split_applied_through is None

    session.add(DailyPrice(company_id=cid, date=split_date, close=121.79))
    session.flush()
    assert len(apply_splits_to_open_positions(session, today=split_date)) == 1
    assert position.entry_price == 120.0
    assert position.stop_loss_price == 90.0


def test_staged_entry_uses_raw_reference_and_restates_a_split_on_effective_day(session, monkeypatch):
    cid = session.query(Company.id).scalar()
    signal = date(2024, 6, 7)
    effective = date(2024, 6, 10)
    reference = session.query(DailyPrice).filter_by(company_id=cid, date=signal).one()
    reference.adjusted_close = 120.888
    session.flush()

    selector = PortfolioSelector(scoring_date=signal, selection_date=effective)
    candidate = {"company_id": cid, "ticker": "NVDA", "score": 90}
    pick = selector._build_pick(candidate, 1, session)
    assert pick["entry_price"] == 120.888  # The historical selector uses total-return prices.
    monkeypatch.setattr(selector, "select_all", lambda db: {"alpha": [pick]})
    selector.select_and_store(session, cash_signal=SimpleNamespace(cash_pct=0.0, state="NORMAL"))
    position = session.query(PortfolioSelection).one()
    mark = session.query(PortfolioCycleMark).one()
    assert position.entry_price == pytest.approx(1208.88)
    assert position.stop_loss_price == pytest.approx(991.28)
    assert position.split_applied_through == signal
    assert mark.ref_price == position.entry_price
    assert ExitRuleChecker(session, completed_session=signal).check_exits() == []

    assert len(apply_splits_to_open_positions(session, today=effective)) == 1
    assert position.entry_price == pytest.approx(120.888)
    assert position.stop_loss_price == pytest.approx(99.128)
    assert mark.ref_price == position.entry_price
    assert ExitRuleChecker(session, completed_session=effective).check_exits() == []
