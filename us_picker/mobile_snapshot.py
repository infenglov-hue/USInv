"""Export a compact offline snapshot for the Android app."""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import mkstemp
from typing import Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from us_picker import read_service
from us_picker.db.schema import (
    AdjustedMetric,
    Company,
    DailyPrice,
    ScoringResult,
)

SNAPSHOT_SCHEMA_VERSION = 1
  # 2026-05-12: kept at 1 to match v1 APK. The v1 APK's SnapshotImportValidator rejects both unexpected schema_version values AND unexpected tables, so `factor_history_quarterly` was also pulled out below until the v2 APK ships in Sprint 3.
ROOM_DATABASE_VERSION = 13
PRICE_HISTORY_DAYS = 730
DEFAULT_MOBILE_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "mobile_snapshot.db"
REQUIRED_TABLES = (
    "snapshot_metadata",
    "home_summary",
    "open_positions",
    "portfolio_history",
    "companies",
    "scoring_latest",
    "sector_benchmarks",
    "model_performance",
    "adjusted_metrics_latest",
    "price_history_730d",
    # 2026-05-12: v1 APK's SnapshotImportValidator rejects snapshots that
    # contain unexpected tables. `factor_history_quarterly` was added in
    # Sprint 2 §5 for the v2 APK's sparkline UI — but the v2 APK doesn't
    # exist yet, so emitting it just breaks the live APK. Re-enable when
    # Sprint 3 ships.
    # 2026-07-05 (B1): `portfolio_cycle_marks` added for the PWA's
    # rotation-period history under position continuity. DELIBERATE break
    # with the archived v1 APK's import validator: the product is PWA-only
    # since 2026-07-01 (docs/PRODUCT_SCOPE_2026-07-01.md); the frozen APK
    # will reject new snapshots and simply keep its last imported data.
    "portfolio_cycle_marks",
)


def _sqlite_value(value: Any) -> Any:
    """Normalize values before writing them into SQLite."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _write_records(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    records: list[dict[str, Any]],
) -> None:
    if not records:
        return
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
    connection.executemany(
        sql,
        [
            tuple(_sqlite_value(record.get(column)) for column in columns)
            for record in records
        ],
    )


def _coherent_model_performance_records(rows: list[Any]) -> list[dict[str, Any]]:
    """Return the latest continuous NAV segment.

    Older incremental backtest code restarted both NAV series at 100 while
    retaining earlier rows. Publishing both sides of that reset makes the
    mobile/web chart look like one continuous multi-year result when it is
    not. Until a full historical rebuild is available, expose only the most
    recent internally coherent segment.
    """
    records = [dict(row._mapping) if hasattr(row, "_mapping") else dict(row) for row in rows]
    last_reset_index = 0
    inside_reset_run = False
    for index in range(1, len(records)):
        previous = records[index - 1]
        current = records[index]
        current_is_reset = (
            abs(float(current.get("strategy_return") or 0.0) - 100.0) < 1e-9
            and abs(float(current.get("benchmark_return") or 0.0) - 100.0) < 1e-9
        )
        previous_was_not_base = (
            abs(float(previous.get("strategy_return") or 0.0) - 100.0) > 1e-6
            or abs(float(previous.get("benchmark_return") or 0.0) - 100.0) > 1e-6
        )
        if current_is_reset and previous_was_not_base:
            last_reset_index = index
            inside_reset_run = True
        elif current_is_reset and inside_reset_run:
            # Repeated incremental runs may leave several consecutive 100/100
            # base rows. The final base row is the actual start of the latest
            # continuous segment.
            last_reset_index = index
        elif not current_is_reset:
            inside_reset_run = False
    return records[last_reset_index:]


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE snapshot_metadata (
            id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL,
            exported_at TEXT NOT NULL,
            snapshot_date TEXT,
            latest_price_date TEXT,
            source_db_path TEXT,
            company_count INTEGER NOT NULL,
            scoring_row_count INTEGER NOT NULL,
            price_history_days INTEGER NOT NULL
        );

        CREATE TABLE home_summary (
            id INTEGER NOT NULL PRIMARY KEY,
            total_return_avg REAL,
            active_return_avg REAL,
            win_rate REAL,
            cumulative_return_pct REAL,
            benchmark_cumulative_return_pct REAL,
            alpha_pct REAL,
            performance_method TEXT,
            return_basis TEXT,
            period_count INTEGER,
            closed_trade_count INTEGER,
            active_position_count INTEGER,
            benchmark_ytd REAL,
            macro_date TEXT,
            policy_rate_pct REAL,
            cpi_yoy_pct REAL,
            usdtry_rate REAL,
            bond_yield_10y_pct REAL,
            high_yield_oas_bps REAL,
            regime TEXT,
            cash_state TEXT,
            cash_pct REAL,
            cash_days_in_state INTEGER,
            cash_last_transition_date TEXT,
            cash_target_state TEXT,
            cash_notes TEXT,
            cash_raw_signal INTEGER
        );


        CREATE TABLE open_positions (
            sort_order INTEGER NOT NULL PRIMARY KEY,
            portfolio TEXT,
            ticker TEXT NOT NULL,
            name TEXT,
            company_id INTEGER,
            entry_price REAL,
            current_price REAL,
            pnl_pct REAL,
            target_price REAL,
            target_source TEXT,
            stop_loss_price REAL,
            stop_pct_from_entry REAL,
            composite_score REAL,
            selection_date TEXT,
            signal_date TEXT,
            days_held INTEGER,
            reason_top_factors_json TEXT,
            quality_flags_json TEXT,
            dcf_margin_of_safety_pct REAL,
            dcf_intrinsic_value REAL,
            dcf_growth_rate_pct REAL,
            dcf_discount_rate_pct REAL,
            dcf_terminal_growth_pct REAL,
            -- B1 continuity (additive): entry_price is the ORIGINAL cost
            -- basis; cycle_ref_* anchor the current rotation period.
            cycle_ref_date TEXT,
            cycle_signal_date TEXT,
            cycle_ref_price REAL,
            period_pnl_pct REAL
        );

        CREATE TABLE portfolio_cycle_marks (
            sort_order INTEGER NOT NULL PRIMARY KEY,
            portfolio TEXT,
            cycle_date TEXT NOT NULL,
            signal_date TEXT,
            company_id INTEGER,
            ticker TEXT,
            ref_price REAL
        );

        CREATE TABLE portfolio_history (
            sort_order INTEGER NOT NULL PRIMARY KEY,
            portfolio TEXT,
            ticker TEXT NOT NULL,
            name TEXT,
            selection_date TEXT,
            signal_date TEXT,
            exit_date TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            holding_days INTEGER
        );

        CREATE TABLE companies (
            id INTEGER NOT NULL PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE,
            name TEXT,
            company_type TEXT,
            sector_bist TEXT,
            sector_custom TEXT,
            is_bist100 INTEGER NOT NULL,
            is_ipo INTEGER NOT NULL,
            free_float_pct REAL,
            listing_date TEXT,
            is_active INTEGER NOT NULL
        );

        CREATE TABLE scoring_latest (
            company_id INTEGER NOT NULL PRIMARY KEY,
            ticker TEXT NOT NULL UNIQUE,
            name TEXT,
            type TEXT,
            sector TEXT,
            is_bist100 INTEGER NOT NULL,
            is_active INTEGER NOT NULL,
            free_float_pct REAL,
            avg_volume_try REAL,
            -- v2 (2026-05-08): unified ranking surface for the APK Liste tab.
            -- ranking_score = composite_alpha for now; ranking_source labels the
            -- pipeline so future ML / blended scores can plug in without
            -- breaking APK assumptions.
            ranking_score REAL,
            ranking_source TEXT,
            -- model_score = the sector-specific composite when one applies
            -- (banking/holding/REIT). NULL for OPERATING. Lets the APK detail
            -- card show "Banking model: 95.3" when relevant.
            model_score REAL,
            alpha REAL,
            -- alpha_x_* (v2): research-quality variant. alpha_x_score weighs
            -- alpha by data confidence; alpha_x_rank is rank within the
            -- research-eligible set; alpha_x_eligible widens core to also
            -- include Quality / Free-Float Shadow buckets so the APK has a
            -- "wider net" view without losing the strict ALPHA Core gate.
            alpha_x_score REAL,
            alpha_x_rank REAL,
            alpha_x_confidence REAL,
            alpha_core_eligible INTEGER NOT NULL,
            alpha_x_eligible INTEGER NOT NULL,
            alpha_reason TEXT,
            alpha_primary_blocker TEXT,
            alpha_research_bucket TEXT,
            alpha_snapshot_streak INTEGER,
            ai_insight TEXT,
            risk TEXT,
            data_completeness REAL,
            scoring_date TEXT,
            pipeline_version TEXT,
            model_used TEXT,
            buffett REAL,
            graham REAL,
            piotroski REAL,
            piotroski_raw INTEGER,
            magic_formula REAL,
            lynch_peg REAL,
            dcf_mos REAL,
            momentum REAL,
            insider REAL,
            technical REAL,
            above_200ma INTEGER,
            dividend REAL,
            beta REAL,
            delta REAL,
            quality_flags_json TEXT,
            dcf_intrinsic_value REAL,
            dcf_growth_rate_pct REAL,
            dcf_discount_rate_pct REAL,
            dcf_terminal_growth_pct REAL,
            target_price REAL,
            target_source TEXT,
            stop_loss_price REAL
        );

        CREATE TABLE sector_benchmarks (
            sector TEXT NOT NULL PRIMARY KEY,
            roe_median REAL,
            roa_median REAL,
            net_margin_median REAL,
            company_count INTEGER
        );

        CREATE TABLE model_performance (
            date TEXT NOT NULL PRIMARY KEY,
            strategy_return REAL,
            benchmark_return REAL,
            alpha REAL
        );

        CREATE TABLE adjusted_metrics_latest (
            company_id INTEGER NOT NULL PRIMARY KEY,
            period_end TEXT,
            source_period_type TEXT,
            calculation_basis TEXT,
            source_periods_json TEXT,
            input_hash TEXT,
            reported_net_income REAL,
            monetary_gain_loss REAL,
            adjusted_net_income REAL,
            owner_earnings REAL,
            free_cash_flow REAL,
            roe_adjusted REAL,
            roa_adjusted REAL,
            eps_adjusted REAL,
            real_eps_growth_pct REAL,
            related_party_revenue_pct REAL,
            maintenance_capex REAL,
            growth_capex REAL
        );

        CREATE TABLE factor_history_quarterly (
            company_id INTEGER NOT NULL,
            quarter_end TEXT NOT NULL,
            scoring_date TEXT NOT NULL,
            buffett REAL,
            graham REAL,
            piotroski REAL,
            magic_formula REAL,
            lynch_peg REAL,
            dcf_mos REAL,
            momentum REAL,
            technical REAL,
            dividend REAL,
            composite_alpha REAL,
            data_completeness REAL,
            PRIMARY KEY(company_id, quarter_end)
        );

        CREATE TABLE price_history_730d (
            company_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            adjusted_close REAL,
            PRIMARY KEY (company_id, date)
        );

        """
    )


def _load_companies(session) -> list[Company]:
    return session.query(Company).order_by(Company.ticker.asc()).all()


def _load_latest_scores(session, scoring_date: date) -> dict[int, ScoringResult]:
    rows = (
        session.query(ScoringResult)
        .filter(ScoringResult.scoring_date == scoring_date)
        .all()
    )
    return {row.company_id: row for row in rows}


def _load_latest_adjusted_metrics(session) -> dict[int, AdjustedMetric]:
    latest_periods = (
        session.query(
            AdjustedMetric.company_id.label("company_id"),
            func.max(AdjustedMetric.period_end).label("period_end"),
        )
        .group_by(AdjustedMetric.company_id)
        .subquery()
    )
    rows = (
        session.query(AdjustedMetric)
        .join(
            latest_periods,
            (AdjustedMetric.company_id == latest_periods.c.company_id)
            & (AdjustedMetric.period_end == latest_periods.c.period_end),
        )
        .all()
    )
    return {row.company_id: row for row in rows}


def _select_factor_history_universe(
    companies_by_ticker: dict,
    open_positions_frame,
    scoring_frame,
    top_alpha_n: int = 75,
) -> list[int]:
    """Pick the company set worth shipping quarter-end factor history for.

    Universe = currently-open ALPHA positions ∪ top-N alpha_x_eligible
    companies in the latest scoring snapshot. Anything outside this set
    won't get a sparkline in the APK, so storing 8 quarters of factor
    history for it is wasted bytes.

    Returns a list of unique company_ids; keeps the snapshot footprint
    bounded (~75 companies × 8 quarters × ~12 numeric columns ≈ 60 KB).
    """
    ids: set[int] = set()

    if open_positions_frame is not None and not open_positions_frame.empty:
        for ticker in open_positions_frame.get("ticker", []):
            company = companies_by_ticker.get(str(ticker))
            if company is not None:
                ids.add(company.id)

    if scoring_frame is not None and not scoring_frame.empty:
        eligible = scoring_frame
        if "alpha_x_eligible" in eligible.columns:
            eligible = eligible[eligible["alpha_x_eligible"].astype(bool)]
        if "alpha_x_score" in eligible.columns:
            eligible = eligible.sort_values(
                "alpha_x_score", ascending=False, na_position="last"
            )
        for ticker in eligible.head(top_alpha_n).get("ticker", []):
            company = companies_by_ticker.get(str(ticker))
            if company is not None:
                ids.add(company.id)

    return sorted(ids)


def _load_price_history(session) -> list[dict[str, Any]]:
    cutoff = date.today() - timedelta(days=PRICE_HISTORY_DAYS)
    rows = (
        session.query(DailyPrice)
        .filter(DailyPrice.date >= cutoff)
        .order_by(DailyPrice.company_id.asc(), DailyPrice.date.asc())
        .all()
    )
    return [
        {
            "company_id": row.company_id,
            "date": row.date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "adjusted_close": row.adjusted_close,
        }
        for row in rows
    ]


def _market_wide_latest_price_date(
    price_history: list[dict[str, Any]],
    companies: list[Company],
) -> date | None:
    """Return the latest broad equity close date, ignoring index-only updates."""
    active_equity_ids = {
        company.id
        for company in companies
        if company.is_active
        and company.ticker != "SPY"
        and str(company.company_type or "").upper() != "INDEX"
    }
    if not active_equity_ids:
        active_equity_ids = {
            company.id
            for company in companies
            if company.ticker != "SPY"
            and str(company.company_type or "").upper() != "INDEX"
        }

    latest_by_company: dict[int, date] = {}
    for row in price_history:
        company_id = row.get("company_id")
        price_date = row.get("date")
        if company_id not in active_equity_ids or price_date is None:
            continue
        previous = latest_by_company.get(company_id)
        if previous is None or price_date > previous:
            latest_by_company[company_id] = price_date

    if not latest_by_company:
        return max((row["date"] for row in price_history if row.get("date")), default=None)

    date_counts = Counter(latest_by_company.values())
    max_coverage = max(date_counts.values(), default=0)
    coverage_floor = max(1, int(max_coverage * 0.5))
    for price_date in sorted(date_counts.keys(), reverse=True):
        if date_counts[price_date] >= coverage_floor:
            return price_date
    return max(date_counts.keys(), default=None)


def export_mobile_snapshot(output_path: str | Path = DEFAULT_MOBILE_SNAPSHOT_PATH) -> Path:
    """Export the latest offline mobile snapshot into a compact SQLite database."""
    latest_scoring_date = read_service.get_latest_scoring_date()
    if latest_scoring_date is None:
        raise RuntimeError("No scoring snapshot found. Run the scoring pipeline first.")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).replace(microsecond=0)
    source_engine = read_service.get_engine()
    source_db_path = getattr(source_engine.url, "database", None)
    session = sessionmaker(bind=source_engine)()

    try:
        performance = read_service.get_all_portfolio_performance() or {}
        macro = read_service.get_latest_macro() or {}
        cash_state = read_service.get_latest_cash_state() or {}
        open_positions_frame = read_service.get_open_positions()
        portfolio_history_frame = read_service.get_portfolio_history()
        cycle_marks_frame = read_service.get_cycle_marks()
        scoring_frame = read_service.get_scoring_results(scoring_date=latest_scoring_date)
        companies = _load_companies(session)
        companies_by_ticker = {company.ticker: company for company in companies}
        latest_scores = _load_latest_scores(session, latest_scoring_date)
        latest_metrics = _load_latest_adjusted_metrics(session)
        price_history = _load_price_history(session)
        
        # Get IDs for top 75 companies to fetch history for
        top_75_tickers = [
            row["ticker"] 
            for row in scoring_frame.sort_values("ranking_score", ascending=False)
            .head(75)
            .to_dict(orient="records")
        ]
        top_75_ids = [
            companies_by_ticker[t].id 
            for t in top_75_tickers 
            if t in companies_by_ticker
        ]
        
        factor_history_frame = read_service.get_factor_history_quarterly(
            company_ids=top_75_ids,
            quarters=8,
        )
    finally:
        session.close()

    latest_price_date = _market_wide_latest_price_date(price_history, companies)

    temp_fd, temp_name = mkstemp(
        prefix="mobile_snapshot_",
        suffix=".db",
        dir=str(output_path.parent),
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    connection: sqlite3.Connection | None = None

    try:
        connection = sqlite3.connect(temp_path)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute(f"PRAGMA user_version = {ROOM_DATABASE_VERSION}")
        
        # Room Identity Hash support
        connection.execute("CREATE TABLE IF NOT EXISTS room_master_table (id INTEGER PRIMARY KEY,identity_hash TEXT)")
        connection.execute("INSERT OR REPLACE INTO room_master_table (id,identity_hash) VALUES(42, '6bef0c547b56ea37cf66a172bff7d5ea')")

        _create_schema(connection)

        _write_records(
            connection,
            "snapshot_metadata",
            [
                "id",
                "schema_version",
                "exported_at",
                "snapshot_date",
                "latest_price_date",
                "source_db_path",
                "company_count",
                "scoring_row_count",
                "price_history_days",
            ],
            [
                {
                    "id": 1,
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "exported_at": exported_at,
                    "snapshot_date": latest_scoring_date,
                    "latest_price_date": latest_price_date,
                    "source_db_path": source_db_path,
                    "company_count": len(companies),
                    "scoring_row_count": len(scoring_frame),
                    "price_history_days": PRICE_HISTORY_DAYS,
                }
            ],
        )

        _write_records(
            connection,
            "home_summary",
            [
                "id",
                "total_return_avg",
                "active_return_avg",
                "win_rate",
                "cumulative_return_pct",
                "benchmark_cumulative_return_pct",
                "alpha_pct",
                "performance_method",
                "return_basis",
                "period_count",
                "closed_trade_count",
                "active_position_count",
                "benchmark_ytd",
                "macro_date",
                "policy_rate_pct",
                "cpi_yoy_pct",
                "usdtry_rate",
                "bond_yield_10y_pct",
                "high_yield_oas_bps",
                "regime",
                "cash_state",
                "cash_pct",
                "cash_days_in_state",
                "cash_last_transition_date",
                "cash_target_state",
                "cash_notes",
                "cash_raw_signal",
            ],
            [
                {
                    "id": 1,
                    "total_return_avg": performance.get("total_return_avg"),
                    "active_return_avg": performance.get("active_return_avg"),
                    "win_rate": performance.get("win_rate"),
                    "cumulative_return_pct": performance.get("cumulative_return_pct"),
                    "benchmark_cumulative_return_pct": performance.get(
                        "benchmark_cumulative_return_pct"
                    ),
                    "alpha_pct": performance.get("alpha_pct"),
                    "performance_method": performance.get("performance_method"),
                    "return_basis": performance.get("return_basis"),
                    "period_count": performance.get("period_count"),
                    "closed_trade_count": performance.get("closed_trade_count"),
                    "active_position_count": performance.get("active_position_count"),
                    "benchmark_ytd": performance.get("benchmark_ytd"),
                    "macro_date": macro.get("date"),
                    "policy_rate_pct": macro.get("policy_rate_pct"),
                    "cpi_yoy_pct": macro.get("cpi_yoy_pct"),
                    "usdtry_rate": macro.get("usdtry_rate"),
                    # US port: 10y Treasury and HY OAS (stored in turkey_cds_5y).
                    "bond_yield_10y_pct": macro.get("bond_yield_10y_pct"),
                    "high_yield_oas_bps": macro.get("turkey_cds_5y"),
                    "regime": macro.get("regime"),
                    "cash_state": cash_state.get("state"),
                    "cash_pct": cash_state.get("cash_pct"),
                    "cash_days_in_state": cash_state.get("days_in_state"),
                    "cash_last_transition_date": cash_state.get("last_transition_date"),
                    "cash_target_state": cash_state.get("target_state"),
                    "cash_notes": cash_state.get("notes"),
                    "cash_raw_signal": cash_state.get("raw_signal"),
                }
            ],
        )

        _write_records(
            connection,
            "open_positions",
            [
                "sort_order",
                "portfolio",
                "ticker",
                "name",
                "company_id",
                "entry_price",
                "current_price",
                "pnl_pct",
                "target_price",
                "target_source",
                "stop_loss_price",
                "stop_pct_from_entry",
                "composite_score",
                "selection_date",
                "signal_date",
                "days_held",
                "reason_top_factors_json",
                "quality_flags_json",
                "dcf_margin_of_safety_pct",
                "dcf_intrinsic_value",
                "dcf_growth_rate_pct",
                "dcf_discount_rate_pct",
                "dcf_terminal_growth_pct",
                "cycle_ref_date",
                "cycle_signal_date",
                "cycle_ref_price",
                "period_pnl_pct",
            ],
            [
                {"sort_order": index, **record}
                for index, record in enumerate(
                    open_positions_frame.to_dict(orient="records"),
                    start=1,
                )
            ],
        )

        _write_records(
            connection,
            "portfolio_cycle_marks",
            [
                "sort_order",
                "portfolio",
                "cycle_date",
                "signal_date",
                "company_id",
                "ticker",
                "ref_price",
            ],
            [
                {"sort_order": index, **record}
                for index, record in enumerate(
                    cycle_marks_frame.to_dict(orient="records"),
                    start=1,
                )
            ],
        )

        _write_records(
            connection,
            "portfolio_history",
            [
                "sort_order",
                "portfolio",
                "ticker",
                "name",
                "selection_date",
                "signal_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "pnl_pct",
                "exit_reason",
                "holding_days",
            ],
            [
                {"sort_order": index, **record}
                for index, record in enumerate(
                    portfolio_history_frame.to_dict(orient="records"),
                    start=1,
                )
            ],
        )

        _write_records(
            connection,
            "companies",
            [
                "id",
                "ticker",
                "name",
                "company_type",
                "sector_bist",
                "sector_custom",
                "is_bist100",
                "is_ipo",
                "free_float_pct",
                "listing_date",
                "is_active",
            ],
            [
                {
                    "id": company.id,
                    "ticker": company.ticker,
                    "name": company.name,
                    "company_type": company.company_type,
                    "sector_bist": company.sector_bist,
                    "sector_custom": company.sector_custom,
                    "is_bist100": company.is_bist100,
                    "is_ipo": company.is_ipo,
                    "free_float_pct": company.free_float_pct,
                    "listing_date": company.listing_date,
                    "is_active": company.is_active,
                }
                for company in companies
            ],
        )

        _write_records(
            connection,
            "scoring_latest",
            [
                "company_id",
                "ticker",
                "name",
                "type",
                "sector",
                "is_bist100",
                "is_active",
                "free_float_pct",
                "avg_volume_try",
                "ranking_score",
                "ranking_source",
                "model_score",
                "alpha",
                "alpha_x_score",
                "alpha_x_rank",
                "alpha_x_confidence",
                "alpha_core_eligible",
                "alpha_x_eligible",
                "alpha_reason",
                "alpha_primary_blocker",
                "alpha_research_bucket",
                "alpha_snapshot_streak",
                "ai_insight",
                "risk",

                "data_completeness",
                "scoring_date",
                "pipeline_version",
                "model_used",
                "buffett",
                "graham",
                "piotroski",
                "piotroski_raw",
                "magic_formula",
                "lynch_peg",
                "dcf_mos",
                "momentum",
                "insider",
                "technical",
                "above_200ma",
                "dividend",
                "beta",
                "delta",
                "quality_flags_json",
                "dcf_intrinsic_value",
                "dcf_growth_rate_pct",
                "dcf_discount_rate_pct",
                "dcf_terminal_growth_pct",
                "target_price",
                "target_source",
                "stop_loss_price",
            ],
            [
                {
                    "company_id": companies_by_ticker[str(record["ticker"])].id,
                    "ticker": record.get("ticker"),
                    "name": record.get("name"),
                    "type": record.get("type"),
                    "sector": record.get("sector"),
                    "is_bist100": record.get("bist100"),
                    "is_active": record.get("is_active"),
                    "free_float_pct": record.get("free_float_pct"),
                    "avg_volume_try": record.get("avg_volume_try"),
                    "ranking_score": record.get("ranking_score"),
                    "ranking_source": record.get("ranking_source"),
                    "model_score": record.get("model_score"),
                    "alpha": record.get("alpha"),
                    "alpha_x_score": record.get("alpha_x_score"),
                    "alpha_x_rank": record.get("alpha_x_rank"),
                    "alpha_x_confidence": record.get("alpha_x_confidence"),
                    "alpha_core_eligible": record.get("alpha_core_eligible"),
                    "alpha_x_eligible": record.get("alpha_x_eligible"),
                    "alpha_reason": record.get("alpha_reason"),
                    "alpha_primary_blocker": record.get("alpha_primary_blocker"),
                    "alpha_research_bucket": record.get("alpha_research_bucket"),
                    "alpha_snapshot_streak": record.get("alpha_snapshot_streak"),
                    "ai_insight": record.get("ai_insight"),
                    "risk": record.get("risk"),
                    "data_completeness": record.get("data_completeness"),
                    "scoring_date": record.get("scoring_date"),
                    "pipeline_version": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).pipeline_version
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "model_used": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).model_used
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "buffett": record.get("buffett"),
                    "graham": record.get("graham"),
                    "piotroski": record.get("piotroski"),
                    "piotroski_raw": record.get("piotroski_raw"),
                    "magic_formula": record.get("magic_formula"),
                    "lynch_peg": record.get("lynch_peg"),
                    "dcf_mos": record.get("dcf_mos"),
                    "momentum": record.get("momentum"),
                    "insider": record.get("insider"),
                    "technical": record.get("technical"),
                    "above_200ma": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).above_200ma
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "dividend": record.get("dividend"),
                    "beta": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).composite_beta
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "delta": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).composite_delta
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "quality_flags_json": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).quality_flags_json
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "dcf_intrinsic_value": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).dcf_intrinsic_value
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "dcf_growth_rate_pct": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).dcf_growth_rate_pct
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "dcf_discount_rate_pct": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).dcf_discount_rate_pct
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "dcf_terminal_growth_pct": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).dcf_terminal_growth_pct
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "target_price": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).target_price
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "target_source": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).target_source
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                    "stop_loss_price": (
                        latest_scores.get(companies_by_ticker[str(record["ticker"])].id).stop_loss_price
                        if latest_scores.get(companies_by_ticker[str(record["ticker"])].id)
                        else None
                    ),
                }
                for record in scoring_frame.to_dict(orient="records")
                if str(record.get("ticker")) in companies_by_ticker
            ],
        )

        _write_records(
            connection,
            "adjusted_metrics_latest",
            [
                "company_id",
                "period_end",
                "source_period_type",
                "calculation_basis",
                "source_periods_json",
                "input_hash",
                "reported_net_income",
                "monetary_gain_loss",
                "adjusted_net_income",
                "owner_earnings",
                "free_cash_flow",
                "roe_adjusted",
                "roa_adjusted",
                "eps_adjusted",
                "real_eps_growth_pct",
                "related_party_revenue_pct",
                "maintenance_capex",
                "growth_capex",
            ],
            [
                {
                    "company_id": company_id,
                    "period_end": metric.period_end,
                    "source_period_type": metric.source_period_type,
                    "calculation_basis": metric.calculation_basis,
                    "source_periods_json": metric.source_periods_json,
                    "input_hash": metric.input_hash,
                    "reported_net_income": metric.reported_net_income,
                    "monetary_gain_loss": metric.monetary_gain_loss,
                    "adjusted_net_income": metric.adjusted_net_income,
                    "owner_earnings": metric.owner_earnings,
                    "free_cash_flow": metric.free_cash_flow,
                    "roe_adjusted": metric.roe_adjusted,
                    "roa_adjusted": metric.roa_adjusted,
                    "eps_adjusted": metric.eps_adjusted,
                    "real_eps_growth_pct": metric.real_eps_growth_pct,
                    "related_party_revenue_pct": metric.related_party_revenue_pct,
                    "maintenance_capex": metric.maintenance_capex,
                    "growth_capex": metric.growth_capex,
                }
                for company_id, metric in latest_metrics.items()
            ],
        )

        _write_records(
            connection,
            "price_history_730d",
            [
                "company_id",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "adjusted_close",
            ],
            price_history,
        )

        if factor_history_frame is not None and not factor_history_frame.empty:
            _write_records(
                connection,
                "factor_history_quarterly",
                [
                    "company_id",
                    "quarter_end",
                    "scoring_date",
                    "buffett",
                    "graham",
                    "piotroski",
                    "magic_formula",
                    "lynch_peg",
                    "dcf_mos",
                    "momentum",
                    "technical",
                    "dividend",
                    "composite_alpha",
                    "data_completeness",
                ],
                factor_history_frame.to_dict(orient="records"),
            )

        # ── Model Performance (Backtest) ──────────────────────────────
        from sqlalchemy import text
        try:
            with session.bind.connect() as conn:
                perf_data = conn.execute(
                    text("SELECT date, strategy_return, benchmark_return, alpha FROM model_performance ORDER BY date")
                ).fetchall()
            coherent_perf_data = _coherent_model_performance_records(perf_data)
            
            _write_records(
                connection,
                "model_performance",
                ["date", "strategy_return", "benchmark_return", "alpha"],
                coherent_perf_data,
            )
        except Exception:
            # Table may not exist in environments without a backtest run (e.g. unit tests)
            pass

        # ── Sector Benchmarks ───────────────────────────────────────────
        from us_picker.scoring.benchmarker import Benchmarker
        benchmarker = Benchmarker()
        sector_benchmarks = benchmarker.calculate_sector_medians(session)
        
        _write_records(
            connection,
            "sector_benchmarks",
            ["sector", "roe_median", "roa_median", "net_margin_median"],
            [
                {"sector": sector, **row.to_dict()}
                for sector, row in sector_benchmarks.iterrows()
            ]
        )

        connection.commit()
        connection.close()
        connection = None

        if output_path.exists():
            output_path.unlink()
        os.replace(temp_path, output_path)
    except Exception:
        if connection is not None:
            connection.close()
        if temp_path.exists():
            temp_path.unlink()
        raise

    return output_path


def validate_mobile_snapshot(snapshot_path: str | Path) -> dict[str, Any]:
    """Validate a mobile snapshot file and return its metadata row."""
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)

    with sqlite3.connect(snapshot_path) as connection:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in table_rows}
        missing_tables = [table_name for table_name in REQUIRED_TABLES if table_name not in table_names]
        if missing_tables:
            raise RuntimeError(f"Snapshot missing required tables: {', '.join(missing_tables)}")

        connection.row_factory = sqlite3.Row
        metadata_row = connection.execute(
            "SELECT * FROM snapshot_metadata WHERE id = 1"
        ).fetchone()
        if metadata_row is None:
            raise RuntimeError("Snapshot metadata row is missing.")
        metadata = dict(metadata_row)
        if metadata.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise RuntimeError(
                f"Snapshot schema version {metadata.get('schema_version')} is not supported."
            )
        return metadata
