from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, Company, DailyPrice
from us_picker.utils.index_prices import (
    clear_index_price_cache,
    get_price_splice_factor_by_ticker,
    get_spliced_price_by_ticker,
)


def test_xu100_price_splices_large_source_scale_jump(tmp_path):
    db_path = tmp_path / "prices.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    clear_index_price_cache()
    try:
        xu100 = Company(ticker="SPY", name="BIST 100", company_type="INDEX")
        stock = Company(ticker="TEST", name="Test", company_type="OPERATING")
        session.add_all([xu100, stock])
        session.flush()
        session.add_all([
            DailyPrice(
                company_id=xu100.id,
                date=date(2023, 6, 1),
                close=150.0,
                adjusted_close=150.0,
            ),
            DailyPrice(
                company_id=xu100.id,
                date=date(2023, 6, 2),
                close=5100.0,
                adjusted_close=5100.0,
            ),
            DailyPrice(
                company_id=stock.id,
                date=date(2023, 6, 1),
                close=150.0,
                adjusted_close=150.0,
            ),
        ])
        session.commit()

        assert get_spliced_price_by_ticker(
            session, "SPY", date(2023, 6, 1)
        ) == pytest.approx(5100.0)
        assert get_spliced_price_by_ticker(
            session, "SPY", date(2023, 6, 2)
        ) == pytest.approx(5100.0)
        assert get_spliced_price_by_ticker(
            session, "TEST", date(2023, 6, 1)
        ) == pytest.approx(150.0)
    finally:
        session.close()
        engine.dispose()
        clear_index_price_cache()


def test_stock_price_splices_large_source_scale_jump(tmp_path):
    db_path = tmp_path / "prices.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    clear_index_price_cache()
    try:
        stock = Company(ticker="LINK", name="Link", company_type="OPERATING")
        session.add(stock)
        session.flush()
        session.add_all([
            DailyPrice(
                company_id=stock.id,
                date=date(2024, 2, 19),
                close=8.2927,
                adjusted_close=8.2927,
            ),
            DailyPrice(
                company_id=stock.id,
                date=date(2024, 2, 20),
                close=348.0,
                adjusted_close=348.0,
            ),
        ])
        session.commit()

        assert get_price_splice_factor_by_ticker(
            session, "LINK", date(2024, 2, 19)
        ) == pytest.approx(41.964619, rel=1e-6)
        assert get_spliced_price_by_ticker(
            session, "LINK", date(2024, 2, 19)
        ) == pytest.approx(348.0)
        assert get_spliced_price_by_ticker(
            session, "LINK", date(2024, 2, 20)
        ) == pytest.approx(348.0)
    finally:
        session.close()
        engine.dispose()
        clear_index_price_cache()


def test_stock_price_splices_temporary_bad_scale_bar(tmp_path):
    db_path = tmp_path / "prices.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    clear_index_price_cache()
    try:
        stock = Company(ticker="HALKB", name="Halkbank", company_type="BANK")
        session.add(stock)
        session.flush()
        session.add_all([
            DailyPrice(
                company_id=stock.id,
                date=date(2026, 4, 16),
                close=39.42,
                adjusted_close=39.42,
            ),
            DailyPrice(
                company_id=stock.id,
                date=date(2026, 4, 17),
                close=2.0,
                adjusted_close=2.0,
            ),
            DailyPrice(
                company_id=stock.id,
                date=date(2026, 4, 20),
                close=40.72,
                adjusted_close=40.72,
            ),
        ])
        session.commit()

        assert get_spliced_price_by_ticker(
            session, "HALKB", date(2026, 4, 16)
        ) == pytest.approx(40.72)
        assert get_spliced_price_by_ticker(
            session, "HALKB", date(2026, 4, 17)
        ) == pytest.approx(40.72)
        assert get_spliced_price_by_ticker(
            session, "HALKB", date(2026, 4, 20)
        ) == pytest.approx(40.72)
    finally:
        session.close()
        engine.dispose()
        clear_index_price_cache()
