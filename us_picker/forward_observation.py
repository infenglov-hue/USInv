"""Keep a private record of observed US Picker decisions and market bars."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from us_picker.db.connection import session_scope
from us_picker.db.schema import (
    CashAllocationState,
    Company,
    DailyPrice,
    ForwardObservation,
    PortfolioCycleMark,
    PortfolioSelection,
)
from us_picker.portfolio.execution import latest_completed_session_date
from usinv.calendar import default_calendar


def _record(row, *fields: str) -> dict:
    result = {}
    for name in fields:
        value = getattr(row, name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=UTC).isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, float) and not isfinite(value):
            value = None
        result[name] = value
    return result


def record_observation(session: Session, price_date: date) -> bool:
    """Record the current effective positions, staged decisions, cash state and SPY."""
    next_session = default_calendar().next_session(price_date)
    positions = session.execute(
        select(PortfolioSelection, Company.ticker)
        .join(Company, Company.id == PortfolioSelection.company_id)
        .where(
            PortfolioSelection.portfolio == "ALPHA",
            PortfolioSelection.selection_date <= next_session.label,
            or_(PortfolioSelection.exit_date.is_(None), PortfolioSelection.exit_date >= price_date),
        )
        .order_by(PortfolioSelection.id)
    ).all()
    effective_cycle = session.scalar(
        select(func.max(PortfolioCycleMark.cycle_date)).where(
            PortfolioCycleMark.portfolio == "ALPHA",
            PortfolioCycleMark.cycle_date <= price_date,
        )
    )
    marks = session.execute(
        select(PortfolioCycleMark, Company.ticker)
        .join(Company, Company.id == PortfolioCycleMark.company_id)
        .where(
            PortfolioCycleMark.portfolio == "ALPHA",
            PortfolioCycleMark.cycle_date.between(
                effective_cycle or price_date, next_session.label
            ),
        )
        .order_by(PortfolioCycleMark.cycle_date, PortfolioCycleMark.company_id)
    ).all()
    company_ids = {position.company_id for position, _ in positions}
    bars = session.execute(
        select(DailyPrice, Company.ticker)
        .join(Company, Company.id == DailyPrice.company_id)
        .where(
            DailyPrice.date == price_date, or_(Company.id.in_(company_ids), Company.ticker == "SPY")
        )
        .order_by(Company.ticker)
    ).all()
    if not any(
        ticker == "SPY" and bar.adjusted_close and 0 < bar.adjusted_close < float("inf")
        for bar, ticker in bars
    ):
        raise ValueError(f"SPY total-return close missing for {price_date}")
    cash = session.scalar(
        select(CashAllocationState)
        .where(CashAllocationState.date <= price_date)
        .order_by(CashAllocationState.date.desc())
        .limit(1)
    )
    payload = {
        "version": 1,
        "price_date": price_date.isoformat(),
        "next_session_open": next_session.open_at.isoformat(),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "code_sha": os.environ.get("GITHUB_SHA"),
        "cash": _record(
            cash,
            "date",
            "market_regime",
            "macro_regime",
            "raw_signal",
            "target_state",
            "state",
            "cash_pct",
            "days_in_state",
            "last_transition_date",
            "transitioned_today",
            "notes",
        )
        if cash is not None
        else None,
        "positions": [
            {
                "ticker": ticker,
                **_record(
                    pos,
                    "id",
                    "selection_date",
                    "signal_date",
                    "entry_price",
                    "composite_score",
                    "weight",
                    "cash_state",
                    "cash_pct",
                    "stop_loss_price",
                    "highest_close",
                    "target_price",
                    "target_source",
                    "exit_date",
                    "exit_price",
                    "exit_reason",
                    "cycle_ref_date",
                    "split_applied_through",
                    "reason_top_factors_json",
                    "created_at",
                    "updated_at",
                ),
            }
            for pos, ticker in positions
        ],
        "cycle_marks": [
            {
                "ticker": ticker,
                **_record(mark, "cycle_date", "signal_date", "ref_price", "created_at"),
            }
            for mark, ticker in marks
        ],
        "bars": [
            {
                "ticker": ticker,
                **_record(bar, "open", "high", "low", "close", "adjusted_close", "source"),
            }
            for bar, ticker in bars
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    result = session.execute(
        insert(ForwardObservation)
        .values(
            observation_id=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            price_date=price_date,
            captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
            payload_json=encoded,
        )
        .on_conflict_do_nothing(index_elements=["observation_id"])
    )
    return result.rowcount == 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="Write the private observations as JSON Lines.")
    args = parser.parse_args()
    with session_scope() as session:
        if args.export is not None:
            rows = session.scalars(
                select(ForwardObservation).order_by(
                    ForwardObservation.captured_at,
                    ForwardObservation.observation_id,
                )
            )
            with args.export.open("w", encoding="utf-8") as output:
                for row in rows:
                    item = {"observation_id": row.observation_id, "captured_at": row.captured_at}
                    output.write(
                        json.dumps(item | json.loads(row.payload_json), ensure_ascii=False) + "\n"
                    )
            return
        from us_picker.data.fetcher import _last_complete_session_day

        expected = _last_complete_session_day()
        if latest_completed_session_date(session, expected + timedelta(days=1)) != expected:
            raise SystemExit(f"Missing broad market coverage for completed session {expected}")
        recorded = record_observation(session, expected)
    print(f"Forward observation {'recorded' if recorded else 'already stored'} for {expected}")


if __name__ == "__main__":
    main()
