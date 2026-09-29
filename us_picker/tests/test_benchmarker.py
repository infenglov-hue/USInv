import pytest
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from us_picker.db.schema import Base, Company, AdjustedMetric
from us_picker.scoring.benchmarker import Benchmarker

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # Seed test data
    c1 = Company(id=1, ticker="TEST1", sector_custom="tech", is_active=True)
    c2 = Company(id=2, ticker="TEST2", sector_custom="tech", is_active=True)
    c3 = Company(id=3, ticker="FOOD1", sector_custom="food", is_active=True)
    session.add_all([c1, c2, c3])
    
    m1 = AdjustedMetric(company_id=1, period_end=date(2024, 12, 31), roe_adjusted=0.20, owner_earnings=100, adjusted_net_income=20)
    m2 = AdjustedMetric(company_id=2, period_end=date(2024, 12, 31), roe_adjusted=0.40, owner_earnings=200, adjusted_net_income=60)
    m3 = AdjustedMetric(company_id=3, period_end=date(2024, 12, 31), roe_adjusted=0.10, owner_earnings=100, adjusted_net_income=5)
    session.add_all([m1, m2, m3])
    session.commit()
    
    return session

def test_calculate_sector_medians(session):
    benchmarker = Benchmarker()
    df = benchmarker.calculate_sector_medians(session)
    
    assert "tech" in df.index
    assert "food" in df.index
    
    # Median of 0.20 and 0.40 is 0.30
    assert df.loc["tech", "roe_median"] == pytest.approx(0.30)
    # Median of 0.10 is 0.10
    assert df.loc["food", "roe_median"] == pytest.approx(0.10)
    
    # Net margin: 20/100=0.2, 60/200=0.3 -> median 0.25
    assert df.loc["tech", "net_margin_median"] == pytest.approx(0.25)

