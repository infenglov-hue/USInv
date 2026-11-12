"""Check the private feed without logging portfolio holdings or credentials."""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from math import isfinite

import requests
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from us_picker.data.fetcher import _last_complete_session_day
from us_picker.data.sources.alpaca import AlpacaAuthenticationError, AlpacaClient
from us_picker.db.connection import session_scope
from us_picker.db.schema import Company, DailyPrice, ForwardObservation, PortfolioSelection
from us_picker.portfolio.execution import latest_completed_session_date
from usinv.calendar import default_calendar


def _valid_price(value: float | None) -> bool:
    return value is not None and isfinite(value) and value > 0


def check_authentication() -> None:
    """Fail early for rejected credentials and let the normal fetch handle outages."""
    logging.getLogger("us_picker.data.sources.alpaca").setLevel(logging.CRITICAL)
    try:
        client = AlpacaClient(max_retries=1)
        client.fetch_assets()
        completed = _last_complete_session_day()
        client.fetch_daily_bars(["SPY"], completed, completed)
    except AlpacaAuthenticationError:
        raise SystemExit("::error::Alpaca rejected the configured trading credentials.") from None
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            raise SystemExit(
                f"::error::Alpaca access rejected (HTTP {exc.response.status_code}). "
                "Check the Actions secrets and market-data permissions."
            ) from None
        print("::warning::Alpaca preflight could not finish. The regular refresh will retry.")
    except (requests.RequestException, RuntimeError, ValueError):
        print("::warning::Alpaca preflight could not finish. The regular refresh will retry.")


def _last_observation(session: Session, completed: date) -> date | None:
    return session.scalar(
        select(func.max(ForwardObservation.price_date)).where(
            ForwardObservation.price_date <= completed
        )
    )


def _spy_bars():
    return (
        select(DailyPrice.date)
        .join(Company)
        .where(
            Company.ticker == "SPY",
            DailyPrice.close > 0,
            DailyPrice.close < float("inf"),
            DailyPrice.adjusted_close > 0,
            DailyPrice.adjusted_close < float("inf"),
        )
    )


def price_window(session: Session, completed: date, requested_days: int) -> int:
    """Widen the lookback to cover missed observations and overlap stored prices."""
    anchor = _last_observation(session, completed)
    if anchor is None:
        anchor = session.scalar(
            _spy_bars().where(DailyPrice.date <= completed)
            .order_by(DailyPrice.date.desc()).limit(1)
        )
    if anchor is None:
        return requested_days
    return max(requested_days, (completed - anchor).days + 7)


def verify_prices(session: Session, completed: date) -> None:
    """Require a complete market cutoff and surface missing exit observations."""
    anchor = _last_observation(session, completed)
    start = anchor or completed
    spy_days = set(session.scalars(
        _spy_bars().where(DailyPrice.date.between(start, completed))
    ))
    equity = session.scalar(
        select(Company.id).join(DailyPrice).where(
            DailyPrice.date == completed,
            DailyPrice.adjusted_close > 0,
            DailyPrice.adjusted_close < float("inf"),
            Company.ticker != "SPY",
            Company.is_active.is_(True),
            or_(Company.company_type.is_(None), Company.company_type != "INDEX"),
        ).limit(1)
    )
    if (
        completed not in spy_days
        or equity is None
        or latest_completed_session_date(session, completed + timedelta(days=1)) != completed
    ):
        raise SystemExit(
            "::error::Price refresh lacks broad equity coverage or a valid SPY close "
            f"for completed XNYS session {completed}."
        )

    calendar = default_calendar()
    if anchor is not None and anchor < completed:
        missed_sessions = []
        day = calendar.next_session(anchor).label
        while day < completed:
            missed_sessions.append(day)
            day = calendar.next_session(day).label
        if missed_sessions:
            gaps = sum(day not in spy_days for day in missed_sessions)
            print(
                f"::warning::{len(missed_sessions)} completed XNYS sessions since the last "
                f"recorded observation were missed ({gaps} still lack a SPY bar). "
                "Historical bars do not recreate exit decisions."
            )

    held_closes = list(session.scalars(
        select(DailyPrice.close).select_from(PortfolioSelection)
        .outerjoin(DailyPrice, and_(
            DailyPrice.company_id == PortfolioSelection.company_id, DailyPrice.date == completed
        ))
        .where(
            PortfolioSelection.exit_date.is_(None),
            PortfolioSelection.selection_date <= completed,
            or_(
                PortfolioSelection.cycle_ref_date.is_(None),
                PortfolioSelection.cycle_ref_date <= completed,
            ),
            or_(
                PortfolioSelection.split_applied_through.is_(None),
                PortfolioSelection.split_applied_through <= completed,
            ),
        )
    ))
    missing = sum(not _valid_price(close) for close in held_closes)
    if missing:
        print(
            f"::warning::{missing} of {len(held_closes)} eligible positions lack a valid "
            f"raw close on {completed}; their exit checks have no price."
        )
    print(f"Price coverage verified for {completed} ({len(held_closes) - missing}/{len(held_closes)} exit closes).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("auth")
    window = commands.add_parser("window")
    window.add_argument("days", type=int)
    commands.add_parser("verify")
    args = parser.parse_args()
    if args.command == "auth":
        check_authentication()
        return
    completed = _last_complete_session_day()
    with session_scope() as session:
        if args.command == "window":
            print(price_window(session, completed, args.days))
        else:
            verify_prices(session, completed)


if __name__ == "__main__":
    main()
