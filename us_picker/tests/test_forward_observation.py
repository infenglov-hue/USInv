import json
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Base,
    CashAllocationState,
    Company,
    DailyPrice,
    ForwardObservation,
    PortfolioCycleMark,
    PortfolioSelection,
)
from us_picker.forward_observation import record_observation


def test_observation_preserves_original_prices_stops_and_next_decision(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    friday = date(2026, 11, 13)
    monday = date(2026, 11, 16)
    with Session(engine) as session:
        session.add_all(
            [
                Company(id=1, ticker="HELD"),
                Company(id=2, ticker="NEXT"),
                Company(id=3, ticker="SPY"),
            ]
        )
        session.flush()
        held = PortfolioSelection(
            portfolio="ALPHA",
            company_id=1,
            selection_date=date(2026, 11, 9),
            exit_date=friday,
            exit_reason="STOP_LOSS",
            stop_loss_price=95,
            weight=0.15,
        )
        session.add_all(
            [
                held,
                PortfolioSelection(
                    portfolio="ALPHA", company_id=2, selection_date=monday, signal_date=friday
                ),
                PortfolioCycleMark(
                    portfolio="ALPHA", company_id=2, cycle_date=monday, signal_date=friday
                ),
                DailyPrice(company_id=1, date=friday, close=94, adjusted_close=93),
                DailyPrice(company_id=2, date=monday, close=150, adjusted_close=150),
                DailyPrice(company_id=3, date=friday, close=600, adjusted_close=598),
                CashAllocationState(
                    date=friday,
                    raw_signal=3,
                    target_state="DEFENSIVE",
                    state="CAUTION",
                    cash_pct=0.25,
                ),
            ]
        )
        session.flush()
        assert record_observation(session, friday)
        assert not record_observation(session, friday)
        first = session.scalar(select(ForwardObservation))
        data = json.loads(first.payload_json)
        assert data["next_session_open"].startswith("2026-11-16T09:30")
        assert data["cash"]["target_state"] == "DEFENSIVE"
        assert [
            (p["ticker"], p["selection_date"], p["exit_reason"]) for p in data["positions"]
        ] == [
            ("HELD", "2026-11-09", "STOP_LOSS"),
            ("NEXT", "2026-11-16", None),
        ]
        assert data["cycle_marks"][0]["cycle_date"] == "2026-11-16"
        assert [bar["ticker"] for bar in data["bars"]] == ["HELD", "SPY"]
        held.stop_loss_price = 96
        session.flush()
        assert record_observation(session, friday)
        assert json.loads(first.payload_json)["positions"][0]["stop_loss_price"] == 95


def test_observation_requires_a_current_spy_close():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with (
        Session(engine) as session,
        pytest.raises(ValueError, match="SPY total-return close missing"),
    ):
        record_observation(session, date(2026, 11, 13))
