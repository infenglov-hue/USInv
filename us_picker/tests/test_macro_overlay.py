from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from us_picker.db.schema import Base, MacroRegime
from us_picker.portfolio.macro_overlay import MacroRegimeClassifier


def test_real_rate_penalty_uses_fractional_us_macro_inputs():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    mild = date(2026, 10, 1)
    deep = date(2026, 10, 2)
    with Session(engine) as session:
        session.add_all(
            [
                MacroRegime(date=mild, policy_rate_pct=0.03, cpi_yoy_pct=0.07, turkey_cds_5y=180),
                MacroRegime(date=deep, policy_rate_pct=0.03, cpi_yoy_pct=0.09, turkey_cds_5y=180),
            ]
        )
        session.flush()
        classifier = MacroRegimeClassifier(session)
        assert classifier.classify(mild) == "RISK_ON"
        assert classifier.classify(deep) == "NEUTRAL"
