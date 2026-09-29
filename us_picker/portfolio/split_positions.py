"""Restate open positions for stock splits (US port).

Stored entry/stop/target/high-water prices are in the traded basis at the
time they were written.  When a position's stock splits, those levels must be
divided by the split ratio exactly once, the way a broker restates cost basis.
Per-rotation reference marks of the same company are restated too, so period
returns across the split stay continuous.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    CorporateAction,
    PortfolioCycleMark,
    PortfolioSelection,
)

_PRICE_FIELDS = (
    "entry_price",
    "stop_loss_price",
    "target_price",
    "highest_close",
    "cycle_ref_price",
)


def apply_splits_to_open_positions(
    session: Session, today: Optional[date] = None
) -> list[dict]:
    today = today or date.today()
    events: list[dict] = []
    positions = (
        session.query(PortfolioSelection)
        .filter(PortfolioSelection.exit_date.is_(None))
        .all()
    )
    for position in positions:
        basis = position.split_applied_through or position.selection_date
        splits = (
            session.query(CorporateAction)
            .filter(
                CorporateAction.company_id == position.company_id,
                CorporateAction.action_type == "SPLIT",
                CorporateAction.action_date > basis,
                CorporateAction.action_date <= today,
            )
            .order_by(CorporateAction.action_date)
            .all()
        )
        for split in splits:
            ratio = float(split.adjustment_factor or 0)
            if ratio <= 0 or ratio == 1.0:
                continue
            for field in _PRICE_FIELDS:
                value = getattr(position, field)
                if value is not None:
                    setattr(position, field, value / ratio)
            marks = (
                session.query(PortfolioCycleMark)
                .filter(
                    PortfolioCycleMark.company_id == position.company_id,
                    PortfolioCycleMark.portfolio == position.portfolio,
                    PortfolioCycleMark.cycle_date < split.action_date,
                    PortfolioCycleMark.cycle_date >= position.selection_date,
                )
                .all()
            )
            for mark in marks:
                if mark.ref_price is not None:
                    mark.ref_price = mark.ref_price / ratio
            company = session.get(Company, position.company_id)
            events.append(
                {
                    "ticker": company.ticker if company else str(position.company_id),
                    "ratio": ratio,
                    "action_date": split.action_date,
                }
            )
            position.split_applied_through = split.action_date
    session.flush()
    return events
