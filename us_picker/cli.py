"""CLI entry point for BIST Stock Picker.

Uses Click to provide commands for the full analysis pipeline:
  menu        -> Interactive terminal menu (setup + run + daily ops)
  fetch       -> Stage 1: download price and financial data
  clean       -> Stage 2: inflation-adjust, compute metrics, classify companies
  score       -> Stage 3: calculate and normalize all factor scores
  pick        -> Stage 4: select portfolio stocks
  report      -> Stage 5: display portfolio tables
  run         -> All stages sequentially
  status      -> Current portfolio holdings with P&L
  inspect     -> Deep dive on a single ticker
  check-exits -> Mid-month exit check (stop-loss / target / thesis)

Global flags:
  --verbose   Enable DEBUG logging
  --dry-run   Skip all database writes (read-only pipeline)
"""

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import click
from rich.console import Console

# When run as subprocess from dashboard, PIPE_MODE=1 is set.
PIPE_MODE = os.environ.get("PIPE_MODE") == "1"
if PIPE_MODE:
    import sys
    # In pipe mode: no markup/highlight so output is plain text,
    # and write to a force-flushing stream.
    console = Console(markup=False, highlight=False, force_terminal=False, file=sys.stdout)
else:
    console = Console()


def _week_start_monday(day: date) -> date:
    """Return the Monday for the week containing ``day``."""
    from us_picker.portfolio.rotation import week_start_monday

    return week_start_monday(day)


def _should_skip_portfolio_rotation(
    today: date,
    latest_selection_date: date | None,
    cycle_start: date | None = None,
) -> bool:
    """Return True when the current rotation cycle already has a portfolio.

    Rotation happens once per cycle (every ``selection.rotation_weeks``
    weeks, Monday-anchored). A run on the rotation Monday rotates; any later
    run inside the same cycle acts as dry-run; if the rotation Monday was
    missed entirely (Actions outage), the first run after it catches up once.
    """
    if latest_selection_date is None:
        return False
    if cycle_start is None:
        from us_picker.portfolio.rotation import (
            load_rotation_config,
            rotation_cycle_start,
        )

        weeks, anchor = load_rotation_config()
        cycle_start = rotation_cycle_start(today, weeks, anchor)
    return latest_selection_date >= cycle_start


def _get_engine_and_tables():
    """Create DB engine and ensure all tables exist. Returns the engine."""
    from us_picker.db.connection import ensure_runtime_db_ready, get_engine
    engine = get_engine()
    ensure_runtime_db_ready(engine)
    return engine


# ── Root group ─────────────────────────────────────────────────────────────────

@click.group()
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
@click.option("--dry-run", is_flag=True, help="Skip all database writes.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, dry_run: bool) -> None:
    """BIST Stock Picker -- Buffett-style fundamental analysis for Borsa Istanbul."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["dry_run"] = dry_run

    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── fetch ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--ticker", multiple=True, help="Fetch specific ticker(s) only.")
@click.option("--prices-only", is_flag=True, help="Fetch only price data.")
@click.option(
    "--refresh-universe",
    is_flag=True,
    help="Refresh the company universe before fetching prices. Useful for IPO/new ticker discovery.",
)
@click.option(
    "--price-days",
    type=int,
    default=None,
    help="Price lookback window in days. Useful for daily feed refreshes.",
)
@click.option("--limit", type=int, default=0, help="Limit number of tickers to fetch.")
@click.option(
    "--history",
    is_flag=True,
    help="One-time deep backfill: fetch 5 years of quarterly financial data.",
)
@click.option(
    "--financials-only",
    is_flag=True,
    help=(
        "Fetch only the current year's quarterly financial statements "
        "(freshly filed interim quarters). Cheap; meant for the scheduled "
        "financial-refresh workflow."
    ),
)
@click.pass_context
def fetch(
    ctx: click.Context,
    ticker: tuple,
    prices_only: bool,
    refresh_universe: bool,
    price_days: int | None,
    limit: int,
    history: bool,
    financials_only: bool,
) -> None:
    """Stage 1: Download data from all sources (prices + financials + macro)."""
    from us_picker.db.connection import session_scope
    from us_picker.data.fetcher import DataFetcher

    if financials_only and prices_only:
        raise click.UsageError(
            "--financials-only and --prices-only are mutually exclusive."
        )

    dry_run: bool = ctx.obj.get("dry_run", False)
    if dry_run:
        console.print("[yellow]--dry-run: skipping all fetches.[/yellow]")
        return

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        fetcher = DataFetcher(session=session, console=console)
        tickers = list(ticker) if ticker else None

        if history:
            # One-time deep backfill — financials only, 5 years quarterly
            console.print(
                "[bold magenta]Historical backfill: fetching 5 years of "
                "quarterly financial data...[/bold magenta]"
            )
            if not tickers:
                # Need universe first to know which tickers exist
                fetcher.fetch_universe()
                session.commit()
            fetcher.fetch_history(tickers=tickers)
            return

        if financials_only:
            console.print(
                "[bold blue]Fetching latest quarterly financials only...[/bold blue]"
            )
            if refresh_universe:
                console.print(
                    "[bold blue]Refreshing company universe first...[/bold blue]"
                )
                fetcher.fetch_universe()
                session.commit()
            fetcher.fetch_recent_financials(tickers=tickers)
            return

        if prices_only:
            console.print("[bold blue]Fetching prices only...[/bold blue]")
            if refresh_universe:
                console.print("[bold blue]Refreshing company universe for new listings...[/bold blue]")
                fetcher.fetch_universe()
                session.commit()
            if tickers:
                for t in tickers:
                    fetcher._get_or_create_company(t)
                session.commit()
            fetcher.fetch_prices(tickers=tickers, days_back=price_days or 1500)
        elif tickers:
            console.print(
                f"[bold blue]Fetching data for {', '.join(tickers)}...[/bold blue]"
            )
            fetcher.fetch_universe()
            session.commit()
            fetcher.fetch_prices(tickers=tickers, days_back=price_days or 1500)
            session.commit()
            fetcher.fetch_financials(tickers=tickers)
            session.commit()
            fetcher.fetch_macro()
        else:
            console.print("[bold blue]Fetching all data...[/bold blue]")
            fetcher.fetch_all(limit=limit, price_days=price_days)


# ── check-financial-freshness ─────────────────────────────────────────────────

@cli.command("check-financial-freshness")
@click.option(
    "--min-companies",
    type=int,
    default=None,
    help="A period counts as arrived only with at least this many companies.",
)
@click.option(
    "--grace-days",
    type=int,
    default=None,
    help="Extra slack (days) on top of the SPK deadline before alarming.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def check_financial_freshness_cmd(
    min_companies: int | None, grace_days: int | None, as_json: bool
) -> None:
    """Alarm (exit 1) when quarterly financials lag the SPK filing calendar.

    Compares the newest broadly-populated period_end in financial_statements
    with the newest quarter whose SPK filing deadline (+grace) has passed —
    i.e. the period the scoring PIT filter already expects to see. Used as a
    red-job gate by the financial-refresh workflow.
    """
    import json as _json

    from us_picker.data.freshness import (
        DEFAULT_GRACE_DAYS,
        DEFAULT_MIN_COMPANIES,
        check_financial_freshness,
    )
    from us_picker.db.connection import session_scope

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        report = check_financial_freshness(
            session,
            min_companies=(
                min_companies if min_companies is not None else DEFAULT_MIN_COMPANIES
            ),
            grace_days=grace_days if grace_days is not None else DEFAULT_GRACE_DAYS,
        )

    if as_json:
        click.echo(_json.dumps(report, ensure_ascii=False))
    else:
        status = (
            "[bold green]FRESH[/bold green]"
            if report["fresh"]
            else "[bold red]STALE[/bold red]"
        )
        console.print(f"Financial statement freshness: {status}")
        console.print(
            f"  expected latest period (SPK calendar): {report['expected_period_end']}"
        )
        console.print(
            f"  actual latest period in DB:            {report['actual_period_end']}"
            f" ({report['companies_at_actual']} companies)"
        )
        if not report["fresh"]:
            console.print(
                "  [yellow]Fundamentals lag the filing calendar — run "
                "`bist fetch --financials-only` (or check the financial-refresh "
                "workflow).[/yellow]"
            )

    if not report["fresh"]:
        raise SystemExit(1)


# ── check-score-input-freshness ───────────────────────────────────────────────

@cli.command("check-score-input-freshness")
@click.option(
    "--min-companies",
    type=int,
    default=None,
    help="A score-input period counts only with at least this many companies.",
)
@click.option(
    "--grace-days",
    type=int,
    default=None,
    help="Extra slack (days) on top of the SPK deadline before alarming.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def check_score_input_freshness_cmd(
    min_companies: int | None, grace_days: int | None, as_json: bool
) -> None:
    """Alarm when raw filings have not reached audited annual/TTM score inputs."""

    import json as _json

    from us_picker.data.freshness import (
        DEFAULT_GRACE_DAYS,
        DEFAULT_MIN_COMPANIES,
        check_score_input_freshness,
    )
    from us_picker.db.connection import session_scope

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        report = check_score_input_freshness(
            session,
            min_companies=(
                min_companies if min_companies is not None else DEFAULT_MIN_COMPANIES
            ),
            grace_days=grace_days if grace_days is not None else DEFAULT_GRACE_DAYS,
        )

    if as_json:
        click.echo(_json.dumps(report, ensure_ascii=False))
    else:
        status = (
            "[bold green]FRESH[/bold green]"
            if report["fresh"]
            else "[bold red]STALE[/bold red]"
        )
        console.print(f"Financial score-input freshness: {status}")
        console.print(
            f"  expected latest period (SPK calendar): {report['expected_period_end']}"
        )
        console.print(
            f"  actual latest audited score input:     {report['actual_period_end']}"
            f" ({report['companies_at_actual']} companies)"
        )
        if not report["fresh"]:
            console.print(
                "  [yellow]Raw filings have not reached annual/TTM score inputs — "
                "run `bist clean` and inspect MetricsCalculator errors.[/yellow]"
            )

    if not report["fresh"]:
        raise SystemExit(1)


# ── clean ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def clean(ctx: click.Context) -> None:
    """Stage 2: Classify companies, adjust for inflation, compute clean metrics."""
    from us_picker.db.connection import session_scope
    from us_picker.classification.company_type import CompanyClassifier
    from us_picker.classification.sector_mapper import SectorMapper
    from us_picker.cleaning.financial_prep import MetricsCalculator

    dry_run: bool = ctx.obj.get("dry_run", False)
    engine = _get_engine_and_tables()

    with session_scope(engine) as session:
        # Step 2a: classify company types (OPERATING / BANK / HOLDING / ...)
        console.print("[bold blue]Classifying companies...[/bold blue]")
        classifier = CompanyClassifier()
        stats = classifier.classify_all(session)
        console.print(
            f"  Classified [bold]{stats.get('total', 0)}[/bold] companies: "
            + ", ".join(
                f"{t}: {n}" for t, n in stats.get("by_type", {}).items()
            )
        )

        console.print("[bold blue]Mapping custom sectors...[/bold blue]")
        sector_mapper = SectorMapper()
        sector_stats = sector_mapper.map_all(session)
        console.print(
            f"  Mapped [bold]{sector_stats.get('total', 0)}[/bold] companies "
            f"into [bold]{len(sector_stats.get('by_sector', {}))}[/bold] custom sectors"
        )

        if dry_run:
            console.print("[yellow]--dry-run: rolling back classification.[/yellow]")
            session.rollback()
            return

        session.commit()

        # Step 2a2: corporate-action price adjustment (band-violation
        # detector). Must run BEFORE anything price-derived so momentum /
        # technical / relative-strength never read a bedelsiz as a crash.
        from us_picker.cleaning.price_adjust import (
            load_adjust_config,
            rebuild_adjusted_closes,
        )

        adjust_cfg = load_adjust_config()
        if adjust_cfg.enabled:
            console.print(
                "[bold blue]Adjusting price series for corporate-action "
                "band violations...[/bold blue]"
            )
            adj_stats = rebuild_adjusted_closes(session, config=adjust_cfg)
            console.print(
                f"  {adj_stats['companies_with_events']} companies / "
                f"{adj_stats['events']} events / "
                f"{adj_stats['rows_updated']} rows adjusted"
            )
            session.commit()

        # Step 2b: calculate adjusted metrics
        console.print("[bold blue]Calculating adjusted financial metrics...[/bold blue]")
        calculator = MetricsCalculator(session=session, console=console)
        result = calculator.calculate_all()
        processed = result.get("calculated", 0)
        skipped = result.get("skipped", 0)
        errors = result.get("errors", 0)
        console.print(
            f"  Metrics: [green]{processed}[/green] processed, "
            f"[yellow]{skipped}[/yellow] skipped, "
            f"[red]{errors}[/red] errors"
        )


# ── score ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--use-regime", is_flag=True, help="Use dynamic regime-switching weights.")
@click.option(
    "--as-of-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Point-in-time scoring cutoff (YYYY-MM-DD). Defaults to today.",
)
@click.pass_context
def score(
    ctx: click.Context,
    use_regime: bool,
    as_of_date: datetime | None = None,
) -> None:
    """Stage 3: Calculate, normalize, and compose all factor scores."""
    import pandas as pd
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import Company, ScoringResult
    from us_picker.scoring.factors.buffett import BuffettScorer
    from us_picker.scoring.factors.dcf import DCFScorer
    from us_picker.scoring.factors.graham import GrahamScorer
    from us_picker.scoring.factors.lynch import LynchScorer
    from us_picker.scoring.factors.magic_formula import MagicFormulaScorer
    from us_picker.scoring.factors.momentum import MomentumScorer
    from us_picker.scoring.factors.piotroski import PiotroskiScorer
    from us_picker.scoring.factors.technical import TechnicalScorer
    from us_picker.scoring.normalizer import ScoreNormalizer
    from us_picker.scoring.composer import ScoreComposer
    from us_picker.scoring.context import ScoringContext
    from us_picker.scoring.version import SCORING_PIPELINE_VERSION

    dry_run: bool = ctx.obj.get("dry_run", False)
    engine = _get_engine_and_tables()
    scoring_date = as_of_date.date() if as_of_date is not None else date.today()

    _FACTOR_COLS = [
        "buffett_score", "graham_score", "piotroski_fscore",
        "magic_formula_rank", "lynch_peg_score", "momentum_score",
        "technical_score",
    ]

    with session_scope(engine) as session:
        from us_picker.portfolio.execution import tradable_company_ids

        tradable_ids = tradable_company_ids(session, scoring_date)
        companies = (
            session.query(Company)
            .filter(
                Company.is_active.is_(True),
                Company.id.in_(tradable_ids),
            )
            .all()
        )
        if not companies:
            console.print("[yellow]No active companies found -- run 'bist fetch' first.[/yellow]")
            return

        console.print(
            f"[bold blue]Scoring {len(companies)} companies "
            f"with recent tradable prices (date: {scoring_date})...[/bold blue]"
        )

        # Step 3a: Per-company factor scorers
        buffett = BuffettScorer()
        dcf = DCFScorer()
        graham = GrahamScorer()
        piotroski = PiotroskiScorer()
        lynch = LynchScorer()
        
        # Initialize ScoringContext for bulk data loading
        context = ScoringContext(session, scoring_date)
        all_ids = [c.id for c in companies]
        context.load_data(all_ids)

        raw_scores: dict[int, dict] = {}
        for company in companies:
            cid = company.id
            row: dict = {
                "company_id": cid,
                "model_used": company.company_type or "OPERATING",
            }

            b = buffett.score(cid, session, scoring_date=scoring_date, scoring_context=context)
            row["buffett_score"] = (b or {}).get("buffett_combined")

            d = dcf.score(
                cid,
                session,
                scoring_date=scoring_date,
                scoring_context=context,
            )
            # dcf_margin_of_safety_pct is stored raw (not normalized)
            row["dcf_margin_of_safety_pct"] = (d or {}).get("dcf_combined")
            # Phase 5: persist the DCF breakdown for transparency UI.
            # Growth / discount / terminal are fractions in DCFScorer output
            # (e.g. 0.12 = 12%) — we store them as percent so downstream
            # consumers can render without having to remember the unit.
            if d is not None:
                intrinsic = d.get("intrinsic_value_per_share")
                growth = d.get("growth_rate_used")
                disc = d.get("discount_rate_used")
                term = d.get("terminal_growth_used")
                row["dcf_intrinsic_value"] = intrinsic
                row["dcf_growth_rate_pct"] = (
                    round(growth * 100.0, 2) if growth is not None else None
                )
                row["dcf_discount_rate_pct"] = (
                    round(disc * 100.0, 2) if disc is not None else None
                )
                row["dcf_terminal_growth_pct"] = (
                    round(term * 100.0, 2) if term is not None else None
                )

            g = graham.score(cid, session, scoring_date=scoring_date, scoring_context=context)
            row["graham_score"] = (g or {}).get("graham_combined")

            p = piotroski.score(cid, session, scoring_date=scoring_date, scoring_context=context)
            row["piotroski_fscore"] = (p or {}).get("fscore_total")
            row["piotroski_fscore_raw"] = int((p or {}).get("fscore_total", 0)) if p else None

            lynch_result = lynch.score(
                cid,
                session,
                scoring_date=scoring_date,
                scoring_context=context,
            )
            row["lynch_peg_score"] = (lynch_result or {}).get("peg_score")

            raw_scores[cid] = row

        # Step 3b: Batch-scored factors
        console.print("[dim]  Running Magic Formula...[/dim]")
        mf_scores = MagicFormulaScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in mf_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["magic_formula_rank"] = result.get("magic_formula_score")

        console.print("[dim]  Running Momentum...[/dim]")
        mom_scores = MomentumScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in mom_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["momentum_score"] = result.get("momentum_combined")

        console.print("[dim]  Running Technical...[/dim]")
        tech_scores = TechnicalScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in tech_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["technical_score"] = result.get("technical_score")
                # Persist raw above_200ma so the selector can apply an
                # absolute falling-knife filter (audit MEDIUM #11).
                raw_scores[cid]["above_200ma"] = result.get("above_200ma")

        console.print("[dim]  Running Dividend Yield...[/dim]")
        from us_picker.scoring.factors.dividend import DividendYieldScorer
        div_scores = DividendYieldScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in div_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["dividend_score"] = result.get("dividend_score")

        # Step 3b+: Sector-specific model scorers (banking, holding)
        console.print("[dim]  Running Banking model...[/dim]")
        from us_picker.scoring.models.banking import BankingScorer
        bank_scores = BankingScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in bank_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["banking_composite"] = result.get("banking_composite")
                raw_scores[cid]["data_completeness"] = result.get("data_completeness")

        console.print("[dim]  Running Holding model...[/dim]")
        from us_picker.scoring.models.holding import HoldingScorer
        hold_scores = HoldingScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in hold_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["holding_composite"] = result.get("holding_composite")
                raw_scores[cid]["data_completeness"] = result.get("data_completeness")

        console.print("[dim]  Running REIT model...[/dim]")
        from us_picker.scoring.models.reit import ReitScorer
        reit_scores = ReitScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in reit_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["reit_composite"] = result.get("reit_composite")
                raw_scores[cid]["data_completeness"] = result.get("data_completeness")
                raw_scores[cid]["model_used"] = "REIT"

        console.print("[dim]  Running Insurance model...[/dim]")
        from us_picker.scoring.models.insurance import InsuranceScorer
        ins_scores = InsuranceScorer().score_all(session, scoring_date=scoring_date)
        for cid, result in ins_scores.items():
            if cid in raw_scores:
                raw_scores[cid]["banking_composite"] = result.get("banking_composite")
                raw_scores[cid]["data_completeness"] = result.get("data_completeness")
                raw_scores[cid]["model_used"] = "INSURANCE"

        if dry_run:
            console.print("[yellow]--dry-run: skipping DB writes for scores.[/yellow]")
            return

        # Step 3c: Upsert raw ScoringResult rows
        # Note: dcf_margin_of_safety_pct is stored raw so target-price logic can
        # still use the original margin-of-safety percentage. The composer
        # normalizes it on the fly when building composites.
        _EXTRA_COLS = ["model_used", "dcf_margin_of_safety_pct",
                       "dividend_score", "banking_composite", "holding_composite",
                       "reit_composite", "piotroski_fscore_raw", "data_completeness",
                       # Phase 5: DCF breakdown + red-flag payload.
                       "dcf_intrinsic_value", "dcf_growth_rate_pct",
                       "dcf_discount_rate_pct", "dcf_terminal_growth_pct",
                       "quality_flags_json",
                       # Sprint 1 §3.5 (2026-05-07): raw above_200ma for
                       # absolute falling-knife filter in selector.
                       "above_200ma"]
        console.print("[dim]  Writing raw scores to DB...[/dim]")
        for cid, row in raw_scores.items():
            existing = (
                session.query(ScoringResult)
                .filter_by(company_id=cid, scoring_date=scoring_date)
                .first()
            )
            if existing:
                existing.pipeline_version = SCORING_PIPELINE_VERSION
                for col in _FACTOR_COLS + _EXTRA_COLS:
                    if col in row:
                        setattr(existing, col, row.get(col))
            else:
                session.add(ScoringResult(
                    company_id=cid,
                    scoring_date=scoring_date,
                    pipeline_version=SCORING_PIPELINE_VERSION,
                    model_used=row.get("model_used"),
                    buffett_score=row.get("buffett_score"),
                    graham_score=row.get("graham_score"),
                    piotroski_fscore=row.get("piotroski_fscore"),
                    piotroski_fscore_raw=row.get("piotroski_fscore_raw"),
                    magic_formula_rank=row.get("magic_formula_rank"),
                    lynch_peg_score=row.get("lynch_peg_score"),
                    momentum_score=row.get("momentum_score"),
                    technical_score=row.get("technical_score"),
                    dcf_margin_of_safety_pct=row.get("dcf_margin_of_safety_pct"),
                    dcf_intrinsic_value=row.get("dcf_intrinsic_value"),
                    dcf_growth_rate_pct=row.get("dcf_growth_rate_pct"),
                    dcf_discount_rate_pct=row.get("dcf_discount_rate_pct"),
                    dcf_terminal_growth_pct=row.get("dcf_terminal_growth_pct"),
                    dividend_score=row.get("dividend_score"),
                    banking_composite=row.get("banking_composite"),
                    holding_composite=row.get("holding_composite"),
                    reit_composite=row.get("reit_composite"),
                    data_completeness=row.get("data_completeness"),
                    quality_flags_json=row.get("quality_flags_json"),
                    above_200ma=row.get("above_200ma"),
                ))
        session.commit()

        # Step 3d: Normalize each factor in-place
        console.print("[dim]  Normalizing factor scores...[/dim]")
        rows = (
            session.query(ScoringResult, Company)
            .join(Company, Company.id == ScoringResult.company_id)
            .filter(ScoringResult.scoring_date == scoring_date)
            .all()
        )
        if rows:
            df = pd.DataFrame([
                {
                    "id": sr.id,
                    "sector": c.sector_custom or c.sector_bist or "UNKNOWN",
                    **{col: getattr(sr, col) for col in _FACTOR_COLS},
                }
                for sr, c in rows
            ]).set_index("id")

            normalizer = ScoreNormalizer()
            for col in _FACTOR_COLS:
                if col in df.columns and df[col].notna().any():
                    df[col] = normalizer.normalize_factor(df, col, "sector")

            for row_id, row_data in df.iterrows():
                sr = session.get(ScoringResult, int(row_id))
                if sr:
                    for col in _FACTOR_COLS:
                        val = row_data.get(col)
                        setattr(sr, col, None if pd.isna(val) else float(val))
            session.commit()

        # Step 3e: Classify risk tiers
        console.print("[dim]  Classifying risk tiers...[/dim]")
        try:
            from us_picker.classification.risk_classifier import RiskClassifier
            risk_clf = RiskClassifier()
            risk_clf.classify_all(session, scoring_date=scoring_date)
        except Exception as exc:
            console.print(f"[yellow]Warning: risk classification failed: {exc}[/yellow]")

        # Step 3e+: Phase 5 red flags. Runs AFTER normalization so
        # technical_score thresholds compare against the 0-100 scale.
        # We read everything back from DB (not the in-memory row dict) so
        # the flag set reflects the persisted, normalized values exactly.
        console.print("[dim]  Detecting red flags...[/dim]")
        try:
            from us_picker.scoring.red_flags import detect_flags, serialize_flags
            flag_rows = (
                session.query(ScoringResult)
                .filter(ScoringResult.scoring_date == scoring_date)
                .all()
            )
            for sr in flag_rows:
                flags = detect_flags({
                    "piotroski_fscore_raw": sr.piotroski_fscore_raw,
                    "data_completeness": sr.data_completeness,
                    "dcf_margin_of_safety_pct": sr.dcf_margin_of_safety_pct,
                    "technical_score": sr.technical_score,
                })
                sr.quality_flags_json = serialize_flags(flags)
            session.commit()
        except Exception as exc:
            console.print(f"[yellow]Warning: red flag detection failed: {exc}[/yellow]")

        # Step 3f: Compute composite scores
        console.print("[dim]  Computing composite scores...[/dim]")
        try:
            composer = ScoreComposer()
            composer.compose_all(session, scoring_date=scoring_date, use_regime=use_regime)
        except Exception as exc:
            console.print(f"[yellow]Warning: composite scoring failed: {exc}[/yellow]")

        # Step 3g: Generate AI insights
        console.print("[dim]  Generating AI insights...[/dim]")
        try:
            from us_picker.scoring.ai_analyst import AiAnalyst
            analyst = AiAnalyst()
            analyst.generate_all_insights(session, scoring_date=scoring_date)
        except Exception as exc:
            console.print(f"[yellow]Warning: AI insight generation failed: {exc}[/yellow]")

        total_scored = sum(
            1 for r in raw_scores.values()
            if any(r.get(c) is not None for c in _FACTOR_COLS)
        )
        console.print(
            f"[green]Scored {total_scored}[/green] / {len(companies)} companies."
        )


# ── pick ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--force", is_flag=True, help="Force portfolio rotation even if not Monday.")
@click.option(
    "--strategy",
    type=click.Choice(["classic", "index-aware", "index_aware"], case_sensitive=False),
    default="index-aware",
    show_default=True,
    help="Portfolio selection strategy variant. Main portfolio uses index-aware.",
)
@click.option(
    "--as-of-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Last completed session allowed as model input (T-1).",
)
@click.option(
    "--effective-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Portfolio effective/trade session (T). Defaults to today.",
)
@click.pass_context
def pick(
    ctx: click.Context,
    force: bool,
    strategy: str,
    as_of_date: datetime | None = None,
    effective_date: datetime | None = None,
) -> None:
    """Stage 4: Select stocks per portfolio using composite scores."""
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import PortfolioSelection
    from us_picker.portfolio.selector import PortfolioSelector

    dry_run: bool = ctx.obj.get("dry_run", False)

    engine = _get_engine_and_tables()

    with session_scope(engine) as session:
        effective = (
            effective_date.date() if effective_date is not None else date.today()
        )
        signal_date = as_of_date.date() if as_of_date is not None else effective
        if signal_date > effective:
            raise click.UsageError("--as-of-date cannot be after --effective-date")
        if not dry_run and not force:
            from us_picker.portfolio.rotation import (
                load_rotation_config,
                rotation_cycle_start,
            )

            rotation_weeks, rotation_anchor = load_rotation_config()
            cycle_start = rotation_cycle_start(
                effective, rotation_weeks, rotation_anchor
            )
            latest_row = (
                session.query(PortfolioSelection.selection_date)
                .filter(PortfolioSelection.portfolio == "ALPHA")
                .order_by(PortfolioSelection.selection_date.desc())
                .first()
            )
            latest_selection_date = latest_row[0] if latest_row else None
            cycle_already_prepared = _should_skip_portfolio_rotation(
                effective, latest_selection_date, cycle_start
            )
            if (
                cycle_already_prepared
                and latest_selection_date == effective
                and effective > date.today()
            ):
                console.print(
                    f"[yellow]Refreshing staged pre-open portfolio for "
                    f"{effective} from signal {signal_date}.[/yellow]"
                )
            elif cycle_already_prepared:
                console.print(
                    f"[yellow]Current rotation cycle (start {cycle_start}, "
                    f"{rotation_weeks}w cadence) already has a portfolio. "
                    "Skipping rotation (acting as dry-run).[/yellow]"
                )
                dry_run = True
            elif effective != cycle_start:
                console.print(
                    f"[yellow]Rotation Monday {cycle_start} was missed "
                    f"({rotation_weeks}w cadence). Running one-time catch-up "
                    "rotation.[/yellow]"
                )

        console.print("[bold blue]Selecting portfolio stocks...[/bold blue]")
        console.print(
            f"[dim]Signal cutoff: {signal_date} -> effective session: "
            f"{effective}[/dim]"
        )
        selector = PortfolioSelector(
            scoring_date=signal_date,
            selection_date=effective,
            strategy_variant=strategy,
        )

        if dry_run:
            all_picks = selector.select_all(session)
            for portfolio, picks in all_picks.items():
                tickers = [p["ticker"] for p in picks]
                console.print(
                    f"  {portfolio.upper()}: {', '.join(tickers) if tickers else 'no picks'}"
                    " [dim](dry-run, not stored)[/dim]"
                )
        else:
            all_picks = selector.select_and_store(session)
            for portfolio, picks in all_picks.items():
                tickers = [p["ticker"] for p in picks]
                console.print(
                    f"  {portfolio.upper()}: {', '.join(tickers) if tickers else 'no picks'}"
                )


@cli.command(name="prepare-portfolio")
@click.option(
    "--next-rotation",
    is_flag=True,
    help="Prepare the next configured rotation instead of today's session.",
)
@click.option(
    "--max-days-ahead",
    type=click.IntRange(min=0),
    default=3,
    show_default=True,
    help="No-op when the next rotation is farther away than this.",
)
@click.option(
    "--effective-date",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Explicit effective session (YYYY-MM-DD).",
)
@click.option("--use-regime", is_flag=True, help="Use dynamic regime weights.")
@click.option(
    "--strategy",
    type=click.Choice(["classic", "index-aware", "index_aware"], case_sensitive=False),
    default="index-aware",
    show_default=True,
)
@click.pass_context
def prepare_portfolio(
    ctx: click.Context,
    next_rotation: bool,
    max_days_ahead: int,
    effective_date: datetime | None,
    use_regime: bool,
    strategy: str,
) -> None:
    """Score T-1 completed data and prepare a portfolio effective on T."""
    if next_rotation and effective_date is not None:
        raise click.UsageError(
            "--next-rotation and --effective-date are mutually exclusive"
        )

    today = date.today()
    if effective_date is not None:
        effective = effective_date.date()
    elif next_rotation:
        from us_picker.portfolio.rotation import (
            load_rotation_config,
            next_rotation_date,
        )

        weeks, anchor = load_rotation_config()
        effective = next_rotation_date(today, weeks, anchor)
        days_ahead = (effective - today).days
        if days_ahead > max_days_ahead:
            console.print(
                f"[yellow]Next rotation {effective} is {days_ahead} days away; "
                "nothing prepared.[/yellow]"
            )
            return
    else:
        effective = today

    from us_picker.db.connection import session_scope
    from us_picker.portfolio.execution import (
        latest_completed_session_date,
        stamp_missing_signal_dates,
    )

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        signal_date = latest_completed_session_date(session, effective)
        if signal_date is not None:
            selection_count, mark_count = stamp_missing_signal_dates(
                session,
                effective,
                signal_date,
            )
            if selection_count or mark_count:
                console.print(
                    f"[dim]Stamped legacy {effective} rows with signal "
                    f"{signal_date} ({selection_count} selections, "
                    f"{mark_count} marks).[/dim]"
                )
    if signal_date is None:
        raise click.ClickException(
            f"No completed market session found before {effective}"
        )

    signal_dt = datetime.combine(signal_date, datetime.min.time())
    effective_dt = datetime.combine(effective, datetime.min.time())
    console.print(
        f"[bold blue]Preparing {effective} portfolio from completed "
        f"{signal_date} data (T-1 -> T)...[/bold blue]"
    )
    ctx.invoke(score, use_regime=use_regime, as_of_date=signal_dt)
    ctx.invoke(
        pick,
        force=False,
        strategy=strategy,
        as_of_date=signal_dt,
        effective_date=effective_dt,
    )


# ── report ─────────────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--portfolio",
    type=click.Choice(["alpha", "all"], case_sensitive=False),
    default="all",
    help="Which portfolio to display.",
)
@click.option(
    "--format",
    type=click.Choice(["terminal", "excel"], case_sensitive=False),
    default="terminal",
    help="Output format: terminal table or Excel file.",
)
@click.pass_context
def report(ctx: click.Context, portfolio: str, format: str) -> None:
    """Stage 5: Display portfolio tables or generate Excel report."""
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import Company, PortfolioSelection
    from us_picker.output.terminal import TerminalOutput
    from us_picker.portfolio.selector import get_selection_target_count
    from rich.table import Table
    from rich import box

    engine = _get_engine_and_tables()


    with session_scope(engine) as session:
        if format.lower() == "excel":
            from us_picker.output.excel import ExcelReporter
            reporter = ExcelReporter()
            try:
                path = reporter.generate(session)
                console.print(f"[green]Excel report generated: {path}[/green]")
            except Exception as e:
                console.print(f"[red]Failed to generate Excel report: {e}[/red]")
            return

        # Terminal Output Logic
        from us_picker.output.performance import PerformanceTracker
        
        # Performance Summary
        tracker = PerformanceTracker(session)
        console.print("[bold underline]Performance Summary[/bold underline]")
        
        perf_table = Table(box=box.SIMPLE)
        perf_table.add_column("Portfolio", style="cyan")
        perf_table.add_column("Avg Return", justify="right")
        perf_table.add_column("Win Rate", justify="right")
        
        for p in ["ALPHA"]:
            stats = tracker.calculate_portfolio_performance(p)
            avg = stats.get("total_return_avg", 0.0)
            win = stats.get("win_rate", 0.0)
            color = "green" if avg >= 0 else "red"
            perf_table.add_row(p, f"[{color}]{avg:.1f}%[/{color}]", f"{win:.1f}%")
            
        console.print(perf_table)
        console.print()

        output = TerminalOutput(console=console)
        target_count = get_selection_target_count()
        portfolios_to_show = (
            ["alpha"]
            if portfolio.lower() == "all"
            else [portfolio.lower()]
        )

        for pname in portfolios_to_show:
            # Load most recent selections for this portfolio
            rows = (
                session.query(PortfolioSelection, Company)
                .join(Company, Company.id == PortfolioSelection.company_id)
                .filter(PortfolioSelection.portfolio == pname.upper())
                .order_by(
                    PortfolioSelection.selection_date.desc(),
                    PortfolioSelection.composite_score.desc(),
                )
                .limit(10)
                .all()
            )

            if not rows:
                console.print(
                    f"[yellow]No selections found for {pname.upper()} -- "
                    "run 'bist pick' first.[/yellow]"
                )
                continue

            # Use only picks from the most recent selection_date
            # Safe access if rows is not empty
            most_recent_date = rows[0][0].selection_date 
            picks = []
            for rank, (pos, company) in enumerate(rows, start=1):
                if pos.selection_date != most_recent_date:
                    break
                if len(picks) >= target_count:
                    break
                picks.append({
                    "company_id": company.id,
                    "ticker": company.ticker,
                    "score": pos.composite_score,
                    "rank": rank,
                    "entry_price": pos.entry_price,
                    "target_price": pos.target_price,
                    "stop_loss": pos.stop_loss_price,
                })

            output.show_portfolio(pname, picks, session)


# ── run ────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--ticker", multiple=True, help="Limit pipeline to specific ticker(s).")
@click.option("--use-regime", is_flag=True, help="Use dynamic regime-switching weights.")
@click.option("--force", is_flag=True, help="Force portfolio rotation even if not Monday.")
@click.option(
    "--strategy",
    type=click.Choice(["classic", "index-aware", "index_aware"], case_sensitive=False),
    default="index-aware",
    show_default=True,
    help="Portfolio selection strategy variant. Main portfolio uses index-aware.",
)
@click.pass_context
def run(ctx: click.Context, ticker: tuple, use_regime: bool, force: bool, strategy: str) -> None:
    """Run all pipeline stages sequentially: fetch, clean, score, pick, report."""
    console.print("[bold blue]Running full pipeline...[/bold blue]")
    ctx.invoke(
        fetch,
        ticker=ticker,
        prices_only=False,
        refresh_universe=False,
        price_days=None,
        limit=0,
        history=False,
    )
    ctx.invoke(clean)
    ctx.invoke(score, use_regime=use_regime)
    ctx.invoke(pick, force=force, strategy=strategy)
    ctx.invoke(report, portfolio="all")
    console.print("[bold green]Pipeline complete.[/bold green]")


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show current portfolio holdings with entry price and P&L."""
    from us_picker.db.connection import session_scope
    from us_picker.output.terminal import TerminalOutput

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        output = TerminalOutput(console=console)
        output.show_status(session)


# ── inspect ────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("ticker")
@click.pass_context
def inspect(ctx: click.Context, ticker: str) -> None:
    """Deep dive on a single stock (e.g.: bist inspect THYAO)."""
    from us_picker.db.connection import session_scope
    from us_picker.output.terminal import TerminalOutput

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        output = TerminalOutput(console=console)
        output.show_inspect(ticker, session)


# ── check-exits ────────────────────────────────────────────────────────────────

@cli.command(name="check-exits")
@click.option("--apply", is_flag=True, help="Apply exit signals to the database (sell positions).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt (CI/cron use).")
@click.pass_context
def check_exits(ctx: click.Context, apply: bool, yes: bool) -> None:
    """Mid-month exit check: evaluate stop-loss, target, and thesis-breaker conditions."""
    from rich.table import Table
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import PortfolioSelection
    from us_picker.portfolio.exit_rules import ExitRuleChecker

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        checker = ExitRuleChecker(session)

        # B1: ratchet trailing stops daily BEFORE evaluating exits, so a
        # position that fell through today's raised stop exits this run.
        # Only persisted in --apply mode (advisory runs stay read-only).
        if apply:
            raised = checker.update_trailing_stops()
            if raised:
                console.print(f"[cyan]Trailing stops raised on {raised} position(s).[/cyan]")

        signals = checker.check_exits()

        if not signals:
            console.print("[green]No exit signals found. All positions holding steady.[/green]")
            return

        console.print(f"[bold red]Found {len(signals)} exit signals![/bold red]")
        
        table = Table(title="Exit Signals", style="red")
        table.add_column("Ticker", style="cyan")
        table.add_column("Portfolio", style="magenta")
        table.add_column("Entry Price", justify="right")
        table.add_column("Current Price", justify="right")
        table.add_column("Return %", justify="right")
        table.add_column("Reason", style="bold red")
        table.add_column("Details")

        for s in signals:
            color = "green" if s["return_pct"] >= 0 else "red"
            table.add_row(
                s["ticker"],
                s["portfolio"],
                f"{s['entry_price']:.2f}",
                f"{s['current_price']:.2f}",
                f"[{color}]{s['return_pct']:.1f}%[/{color}]",
                s["reason"],
                s["details"],
            )

        console.print(table)

        if apply:
            if yes or click.confirm(f"\n[bold red]Apply these {len(signals)} exits to the database?[/bold red]", default=False):
                for s in signals:
                    # Update PortfolioSelection row
                    pos = (
                        session.query(PortfolioSelection)
                        .filter(
                            PortfolioSelection.portfolio == s["portfolio"],
                            PortfolioSelection.company_id == s["company_id"],
                            PortfolioSelection.exit_date.is_(None)
                        )
                        .first()
                    )
                    if pos:
                        pos.exit_date = s["price_date"]
                        pos.exit_price = s["current_price"]
                        pos.exit_reason = s["reason"]
                        pos.return_pct = s["return_pct"]
                        console.print(f"  [green]Exited {s['ticker']} ([/green]{s['reason']}[green]).[/green]")
                session.commit()
                console.print("[bold green]Exits applied successfully.[/bold green]")
            else:
                console.print("[yellow]Exits not applied.[/yellow]")


@cli.command(name="risk-sizing")
@click.pass_context
def risk_sizing(ctx: click.Context) -> None:
    """REPORT-ONLY: equal vs risk-adjusted (inverse-vol) weight suggestion (B1-P2).

    Shows how the current equal-weight portfolio would look under inverse-
    volatility sizing. Does NOT change live weights — a decision aid only.
    """
    import math
    from rich.table import Table
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import PortfolioSelection, DailyPrice
    from us_picker.portfolio.sizing import risk_parity_report

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        positions = (
            session.query(PortfolioSelection)
            .filter(
                PortfolioSelection.portfolio == "ALPHA",
                PortfolioSelection.exit_date.is_(None),
            )
            .all()
        )
        if not positions:
            console.print("[yellow]No open ALPHA positions.[/yellow]")
            return

        rows_in = []
        for pos in positions:
            closes = (
                session.query(DailyPrice.close)
                .filter(
                    DailyPrice.company_id == pos.company_id,
                    DailyPrice.close.isnot(None),
                )
                .order_by(DailyPrice.date.desc())
                .limit(252)
                .all()
            )
            prices = [float(c[0]) for c in closes if c[0] and float(c[0]) > 0][::-1]
            vol = None
            if len(prices) > 20:
                rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                vol = (var ** 0.5) * (252 ** 0.5)  # annualized
            rows_in.append({"ticker": pos.ticker, "volatility": vol})

        report = risk_parity_report(rows_in)

        table = Table(title="Risk-adjusted sizing (report only)")
        table.add_column("Ticker", style="cyan")
        table.add_column("Yıllık Vol", justify="right")
        table.add_column("Eşit Ağırlık", justify="right")
        table.add_column("Risk-Parity Öneri", justify="right")
        table.add_column("Fark", justify="right")
        for r in report:
            vol_str = f"{r['volatility']*100:.1f}%" if r["volatility"] is not None else "—"
            color = "green" if r["delta_pct"] >= 0 else "red"
            table.add_row(
                r["ticker"],
                vol_str,
                f"{r['equal_weight_pct']:.1f}%",
                f"{r['risk_weight_pct']:.1f}%",
                f"[{color}]{r['delta_pct']:+.1f}pp[/{color}]",
            )
        console.print(table)
        console.print(
            "[dim]Negatif fark = oynak isim, risk-parity'de daha az ağırlık alır. "
            "Canlı ağırlık DEĞİŞMEDİ — bu sadece karar yardımıdır (backtest'siz uygulama).[/dim]"
        )


@cli.command(name="seed-active-periods")
@click.pass_context
def seed_active_periods(ctx: click.Context) -> None:
    """Seed company_active_periods (survivorship-safe as-of universe).

    Derives listing intervals from listing/delisting dates + observed price/
    score history. Additive: until this runs, the as-of universe uses the
    legacy heuristic (behavior unchanged). After seeding, backtest membership
    becomes interval-precise — A/B against the prior run before treating the
    new NAV as production.
    """
    from us_picker.db.connection import session_scope
    from us_picker.db.active_periods import seed_company_active_periods

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        changed = seed_company_active_periods(session)
        session.commit()
        console.print(
            f"[green]company_active_periods seeded/updated: {changed} row(s).[/green]"
        )
        console.print(
            "[dim]As-of universe now uses the interval table. Re-run the "
            "backtest and A/B vs the prior NAV before shipping live.[/dim]"
        )


# ── backtest ────────────────────────────────────────────────────────

def _parse_selection_overrides(pairs) -> dict | None:
    """Parse repeated ``key=value`` strings into a nested override dict.

    Dotted keys nest one level (``index_aware.incumbent_min_score=84`` →
    ``{"index_aware": {"incumbent_min_score": 84.0}}``). Values are cast to
    int, then float, else kept as strings.
    """
    if not pairs:
        return None
    result: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"--override expects key=value, got {pair!r}")
        value: object = raw.strip()
        try:
            value = int(value)  # type: ignore[assignment]
        except ValueError:
            try:
                value = float(value)  # type: ignore[assignment]
            except ValueError:
                pass
        target = result
        parts = key.strip().split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


def _resolve_backtest_rebalance_weeks(value: int | None) -> int:
    """Resolve backtest cadence from CLI override or live rotation config."""
    if value is not None:
        return max(1, int(value))

    from us_picker.portfolio.rotation import load_rotation_config

    weeks, _ = load_rotation_config()
    return max(1, int(weeks))


def _print_real_return_summary(df) -> None:
    """Print nominal vs real/hard-currency strategy performance (A1).

    Nominal-TRY totals overstate the edge across a hyperinflation window; this
    shows the same NAV deflated by CPI, gram gold and USD so the user can read
    the *purchasing-power* result next to the headline number.
    """
    from us_picker.backtest.engine import BacktestEngine

    try:
        summary = BacktestEngine._summarize_performance(df)
    except Exception as exc:  # pragma: no cover - reporting must never crash a run
        console.print(f"[yellow]Real-return summary unavailable: {exc}[/yellow]")
        return

    def _fmt(value, suffix="%"):
        return f"{value:+.1f}{suffix}" if isinstance(value, (int, float)) else "n/a"

    console.print("\n[bold underline]Getiri: nominal vs reel / sert-para[/bold underline]")
    console.print(
        f"  [bold]Nominal TL[/bold]  toplam {_fmt(summary.get('total_return_pct'))}  |  "
        f"CAGR {_fmt(summary.get('cagr_pct'))}  |  "
        f"MaxDD {_fmt(summary.get('max_drawdown_pct'))}  |  "
        f"Sharpe {_fmt(summary.get('sharpe_nominal'), '')}"
    )
    labels = {
        "cpi_real": "TÜFE-reel ",
        "gram_gold": "Gram altın",
        "usd": "USD       ",
    }
    deflated = summary.get("deflated") or {}
    if not deflated:
        console.print(
            "  [dim]Reel/sert-para kolonları yok (altın/USD/TÜFE verisi çekilemedi).[/dim]"
        )
        return
    for unit_key, label in labels.items():
        unit = deflated.get(unit_key)
        if not unit:
            continue
        console.print(
            f"  [bold]{label}[/bold]  toplam {_fmt(unit.get('total_return_pct'))}  |  "
            f"CAGR {_fmt(unit.get('cagr_pct'))}  |  "
            f"MaxDD {_fmt(unit.get('max_drawdown_pct'))}  |  "
            f"Sharpe {_fmt(unit.get('sharpe'), '')}  |  "
            f"alpha {_fmt(unit.get('alpha_pct'))}"
        )
    console.print(
        "  [dim]Reel/altın/USD alpha = strateji − BIST100 aynı birimde. "
        "Nominal Sharpe hiperenflasyonda yanıltıcıdır; reel Sharpe'a bakın.[/dim]"
    )

    tail = summary.get("tail_risk") or {}
    if any(v is not None for v in tail.values()):
        console.print("\n[bold underline]Kuyruk riski (B1)[/bold underline]")
        console.print(
            f"  Skew {_fmt(tail.get('skew'), '')}  |  "
            f"Excess-kurtosis {_fmt(tail.get('excess_kurtosis'), '')}  |  "
            f"CVaR%5 {_fmt(tail.get('cvar_5pct'))}  |  "
            f"Ulcer {_fmt(tail.get('ulcer_index_pct'))}  |  "
            f"En kötü dönem {_fmt(tail.get('worst_period_pct'))}"
        )
        console.print(
            "  [dim]Negatif skew + yüksek kurtosis = şişman sol kuyruk (ani sert kayıp riski). "
            "CVaR%5 = en kötü %5 dönemin ortalaması.[/dim]"
        )


def _print_execution_realism(meta: dict | None) -> None:
    """Print A4/#9 execution-realism assumptions + capacity estimate."""
    if not meta:
        return
    idle = meta.get("idle_cash_yield", "none")
    extra = meta.get("extra_cost_round_trip_pct") or 0.0
    console.print("\n[bold underline]Uygulama gerçekçiliği (A4/#9)[/bold underline]")
    console.print(
        f"  Boşta nakit getirisi: [bold]{idle}[/bold]  |  "
        f"Ek TR maliyeti (komisyon+BSMV): {extra:.3f}% gidiş-dönüş  |  "
        f"Baz friction: {meta.get('base_friction_round_trip_pct')}%"
    )
    if meta.get("liquidity_impact_enabled"):
        cap = meta.get("capacity_try")
        port = meta.get("capacity_portfolio_try")
        cap_str = f"~{cap:,.0f} TL" if isinstance(cap, (int, float)) else "n/a"
        console.print(
            f"  Likidite-impact: [bold]AÇIK[/bold] (varsayılan portföy {port:,.0f} TL)  |  "
            f"Tahmini kapasite tavanı: [bold]{cap_str}[/bold]"
        )
        console.print(
            "  [dim]Kapasite = en likit-dar hissenin ADV'sinin participation_cap katı; "
            "bu AUM üstünde slippage backtest varsayımını aşar.[/dim]"
        )
    else:
        console.print(
            "  [dim]Likidite-impact kapalı (düz friction). thresholds.yaml "
            "backtest.liquidity_impact.enabled=true ile kapasiteyi ölç.[/dim]"
        )

    stale_dates = int(meta.get("stale_score_cache_dates") or 0)
    pipeline_version = meta.get("scoring_pipeline_version") or "unknown"
    console.print(f"  Scoring pipeline version: [bold]{pipeline_version}[/bold]")
    if stale_dates:
        console.print(
            f"  [bold red]UNTRUSTWORTHY MODEL A/B: {stale_dates} backtest date(s) "
            "used a stale score cache. Recompute with "
            "--rebuild-stale-scores.[/bold red]"
        )


@cli.command()
@click.option("--start-date", default=None, help="Backtest start date (YYYY-MM-DD). Defaults to 3 years ago.")
@click.option("--end-date", default=None, help="Backtest end date (YYYY-MM-DD). Defaults to today.")
@click.option(
    "--full-reset",
    is_flag=True,
    help="Discard stored model-performance points and rebuild from start-date.",
)
@click.option(
    "--rebuild-stale-scores",
    is_flag=True,
    help=(
        "Recompute historical scores whose pipeline version predates the "
        "current quarterly/TTM contract. Maintenance only; may take hours."
    ),
)
@click.option(
    "--strategy",
    type=click.Choice(["classic", "index-aware", "index_aware"], case_sensitive=False),
    default="index-aware",
    show_default=True,
    help="Portfolio selection strategy variant to backtest. Main portfolio uses index-aware.",
)
@click.option(
    "--execution",
    type=click.Choice(
        [
            "next-open",
            "next_open",
            "same-day-open",
            "same_day_open",
            "same-day-close",
            "same_day_close",
        ],
        case_sensitive=False,
    ),
    default="next-open",
    show_default=True,
    help=(
        "Trade execution assumption. same-day-open means T-1 completed "
        "signal data -> first executable open on/after T."
    ),
)
@click.option(
    "--friction-bps",
    type=float,
    default=40.0,
    show_default=True,
    help="Round-trip friction in basis points for newly bought positions.",
)
@click.option(
    "--rebalance-weeks",
    type=int,
    default=None,
    show_default="thresholds.yaml selection.rotation_weeks",
    help=(
        "Rebalance every N weeks (1=weekly, 2=bi-weekly, 4=~monthly). "
        "Defaults to the live rotation cadence in thresholds.yaml."
    ),
)
@click.option(
    "--turnover-threshold",
    type=float,
    default=None,
    help=(
        "Override selection.turnover_threshold (e.g. 0.10). NOTE: only the "
        "classic variant reads this; index-aware turnover protection is "
        "driven by index_aware.incumbent_min_score/incumbent_window — use "
        "--override for those."
    ),
)
@click.option(
    "--override",
    "overrides",
    multiple=True,
    help=(
        "Selection config override as key=value; dotted keys nest (e.g. "
        "index_aware.incumbent_min_score=84). Repeatable."
    ),
)
@click.option(
    "--continuity/--no-continuity",
    "continuity",
    default=True,
    show_default=True,
    help=(
        "B1 position continuity: held positions carry their stop across "
        "rebalances, never re-anchored down (matches live). --no-continuity "
        "restores the pre-B1 stop re-anchoring for A/B comparison."
    ),
)
@click.option(
    "--intra-trailing/--no-intra-trailing",
    "intra_trailing",
    default=None,
    help=(
        "Trail the stop off the highest daily close inside the holding "
        "window. Defaults to selection.trailing_stop.enabled from "
        "thresholds.yaml so the backtest matches live behavior. NEVER use "
        "tight clamps (10-25% destroyed returns — plan B1 Sonuclar)."
    ),
)
@click.option("--trail-min", type=float, default=None,
              help="Min trailing distance (fraction). Default: thresholds.yaml.")
@click.option("--trail-max", type=float, default=None,
              help="Max trailing distance (fraction). Default: thresholds.yaml.")
@click.pass_context
def backtest(
    ctx: click.Context,
    start_date: str,
    end_date: str,
    full_reset: bool,
    rebuild_stale_scores: bool,
    strategy: str,
    execution: str,
    friction_bps: float,
    rebalance_weeks: int,
    turnover_threshold: float,
    overrides: tuple[str, ...],
    continuity: bool,
    intra_trailing: bool,
    trail_min: float,
    trail_max: float,
) -> None:
    """Stage 6: Run walk-forward multi-year backtest and save results to model_performance."""
    from datetime import datetime, timedelta
    from us_picker.db.connection import session_scope
    from us_picker.backtest.engine import BacktestEngine
    from us_picker.db.schema import ModelPerformance

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        resolved_rebalance_weeks = _resolve_backtest_rebalance_weeks(rebalance_weeks)
        from us_picker.portfolio.rotation import load_rotation_config

        _, rebalance_anchor = load_rotation_config()
        initial_strategy_nav = 100.0
        initial_benchmark_nav = 100.0
        previous_tickers: set[str] = set()

        if full_reset:
            session.query(ModelPerformance).delete()
            session.commit()
        else:
            latest_row = (
                session.query(ModelPerformance)
                .order_by(ModelPerformance.date.desc())
                .first()
            )
            if latest_row is not None:
                latest_date = datetime.strptime(latest_row.date, "%Y-%m-%d").date()
                start_date = latest_row.date
                initial_strategy_nav = float(latest_row.strategy_return or 100.0)
                initial_benchmark_nav = float(latest_row.benchmark_return or 100.0)

                # Recover the prior week's simulated holdings for turnover-aware
                # friction. Failure is non-fatal; NAV continuity is the critical
                # invariant and is preserved by the initial NAV values above.
                try:
                    from us_picker.portfolio.selector import PortfolioSelector

                    previous_date = latest_date - timedelta(weeks=resolved_rebalance_weeks)
                    previous_picks = PortfolioSelector(
                        scoring_date=previous_date,
                        strategy_variant=strategy,
                    ).select("ALPHA", session)
                    previous_tickers = {
                        pick["ticker"] for pick in previous_picks
                    }
                except Exception as exc:
                    console.print(
                        "[yellow]Could not reconstruct prior backtest holdings; "
                        f"continuing without turnover carry-over ({exc}).[/yellow]"
                    )

                console.print(
                    "[yellow]Incremental backtest: appending from "
                    f"{latest_row.date} with model NAV={initial_strategy_nav:.2f} "
                    f"and benchmark NAV={initial_benchmark_nav:.2f}.[/yellow]"
                )

        start_d = None
        if start_date:
            try:
                start_d = datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                console.print(f"[red]Error: Invalid start-date format {start_date}. Must be YYYY-MM-DD.[/red]")
                return

        end_d = None
        if end_date:
            try:
                end_d = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                console.print(f"[red]Error: Invalid end-date format {end_date}. Must be YYYY-MM-DD.[/red]")
                return

        selection_overrides = _parse_selection_overrides(overrides)
        if turnover_threshold is not None:
            selection_overrides = selection_overrides or {}
            selection_overrides["turnover_threshold"] = max(0.0, turnover_threshold)

        # Trailing defaults come from thresholds.yaml so a plain `bist
        # backtest` simulates exactly what the live pipeline trades.
        from us_picker.portfolio.exit_rules import _load_trailing_config

        trail_cfg = _load_trailing_config()
        resolved_trailing = (
            trail_cfg["enabled"] if intra_trailing is None else intra_trailing
        )
        resolved_trail_min = trail_cfg["min_pct"] if trail_min is None else trail_min
        resolved_trail_max = trail_cfg["max_pct"] if trail_max is None else trail_max

        backtester = BacktestEngine(session)
        df = backtester.run_1y_backtest(
            start_date=start_d,
            end_date=end_d,
            console=console,
            initial_strategy_nav=initial_strategy_nav,
            initial_benchmark_nav=initial_benchmark_nav,
            previous_tickers=previous_tickers,
            strategy_variant=strategy,
            execution_mode=execution,
            friction_round_trip=max(0.0, friction_bps) / 10_000.0,
            rebalance_every_n_weeks=resolved_rebalance_weeks,
            rebalance_anchor_date=rebalance_anchor,
            selection_overrides=selection_overrides,
            position_continuity=continuity,
            intra_window_trailing=resolved_trailing,
            trail_min_pct=resolved_trail_min,
            trail_max_pct=resolved_trail_max,
            rebuild_stale_scores=rebuild_stale_scores,
        )
        if not df.empty:
            console.print(f"[green]Backtest completed successfully! Saved {len(df)} points.[/green]")
            console.print(df.tail())
            _print_real_return_summary(df)
            _print_execution_realism(getattr(backtester, "_last_run_meta", None))
        else:
            console.print("[red]Backtest failed or returned no results.[/red]")


@cli.command(name="investor-grade-backtest")
@click.option("--start-date", default=None, help="Backtest start date (YYYY-MM-DD). Defaults to 4 years ago.")
@click.option("--end-date", default=None, help="Backtest end date (YYYY-MM-DD). Defaults to today.")
@click.option(
    "--strategy",
    type=click.Choice(["classic", "index-aware", "index_aware"], case_sensitive=False),
    default="index-aware",
    show_default=True,
    help="Portfolio selection strategy variant to backtest. Main portfolio uses index-aware.",
)
@click.option(
    "--no-persist-base",
    is_flag=True,
    help="Do not overwrite model_performance with the base investor-grade run.",
)
@click.option(
    "--execution",
    type=click.Choice(
        ["same-day-open", "same_day_open", "next-open", "next_open"],
        case_sensitive=False,
    ),
    default="same-day-open",
    show_default=True,
    help="Execution contract used by all base/stress cases.",
)
@click.pass_context
def investor_grade_backtest(
    ctx: click.Context,
    start_date: str,
    end_date: str,
    strategy: str,
    no_persist_base: bool,
    execution: str,
) -> None:
    """Run execution/slippage-stress backtest and export the audit JSON."""
    from datetime import datetime, timedelta
    from us_picker.db.connection import session_scope
    from us_picker.backtest.engine import BacktestEngine

    def parse_date(value: str | None, label: str):
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            console.print(f"[red]Error: Invalid {label} format {value}. Must be YYYY-MM-DD.[/red]")
            return "INVALID"

    start_d = parse_date(start_date, "start-date")
    if start_d == "INVALID":
        return
    end_d = parse_date(end_date, "end-date")
    if end_d == "INVALID":
        return
    if end_d is None:
        end_d = datetime.today().date()
    if start_d is None:
        start_d = end_d - timedelta(weeks=208)
        while start_d.weekday() != 0:
            start_d += timedelta(days=1)

    engine = _get_engine_and_tables()
    with session_scope(engine) as session:
        report = BacktestEngine(session).run_investor_grade_suite(
            start_date=start_d,
            end_date=end_d,
            console=console,
            strategy_variant=strategy,
            execution_mode=execution,
            persist_base=not no_persist_base,
        )

    base = report["cases"][0] if report.get("cases") else {}
    console.print(
        "[bold green]Investor-grade backtest completed: "
        f"{base.get('total_return_pct')}% model, "
        f"{base.get('benchmark_return_pct')}% BIST, "
        f"alpha {base.get('alpha_pct')}%.[/bold green]"
    )


# ── push-sheets ────────────────────────────────────────────────────────────────
 
@cli.command(name="push-sheets")
@click.option(
    "--portfolio",
    type=click.Choice(["alpha", "all"], case_sensitive=False),
    default="all",
    help="Which portfolio to push.",
)
@click.option("--sheet-name", default="BIST Portfolio Tracker", help="Name of the Google Sheet file.")
@click.pass_context
def push_sheets(ctx: click.Context, portfolio: str, sheet_name: str) -> None:
    """Stage 5b: Push portfolio picks to Google Sheets."""
    import os
    from us_picker.db.connection import session_scope
    from us_picker.db.schema import Company, PortfolioSelection
    from us_picker.output.google_sheets import GoogleSheetsClient

    # Finding service_account.json
    creds_path = "service_account.json"
    if not os.path.exists(creds_path):
        # Try config folder
        creds_path = "config/service_account.json"
        if not os.path.exists(creds_path):
             console.print("[red]Error: service_account.json not found in root or config/ folder.[/red]")
             return

    client = GoogleSheetsClient(credentials_path=creds_path)
    if not client.client:
        return

    engine = _get_engine_and_tables()
    
    with session_scope(engine) as session:
        portfolios_to_push = (
            ["alpha"]
            if portfolio.lower() == "all"
            else [portfolio.lower()]
        )

        for pname in portfolios_to_push:
            # Load most recent selections
            rows = (
                session.query(PortfolioSelection, Company)
                .join(Company, Company.id == PortfolioSelection.company_id)
                .filter(PortfolioSelection.portfolio == pname.upper())
                .order_by(
                    PortfolioSelection.selection_date.desc(),
                    PortfolioSelection.composite_score.desc(),
                )
                .limit(10)
                .all()
            )

            if not rows:
                console.print(f"[yellow]No selections found for {pname.upper()}.[/yellow]")
                continue

            most_recent_date = rows[0][0].selection_date
            # Format tab name: "Apr 2023 - Alpha"
            tab_name = f"{most_recent_date.strftime('%b %Y')} - {pname.capitalize()}"
            
            picks = []
            for rank, (pos, company) in enumerate(rows, start=1):
                if pos.selection_date != most_recent_date:
                    break
                
                picks.append({
                    "Rank": rank,
                    "Ticker": company.ticker,
                    "Company": company.name,
                    "Score": float(f"{pos.composite_score:.2f}"),
                    "Entry Price": float(f"{pos.entry_price:.2f}"),
                    "Target": float(f"{pos.target_price:.2f}"),
                    "Stop Loss": float(f"{pos.stop_loss_price:.2f}"),
                    "Sector": company.sector_custom or company.sector_bist or "",
                    "Risk": "TBD" # Could join scoring_result to get risk_tier if needed
                })

            console.print(f"[bold blue]Pushing {len(picks)} picks to '{sheet_name}' / '{tab_name}'...[/bold blue]")
            success = client.push_portfolio(sheet_name, tab_name, picks)
            if success:
                console.print(f"[green]Successfully pushed {pname.upper()}.[/green]")
            else:
                console.print(f"[red]Failed to push {pname.upper()}.[/red]")


@cli.command(name="export-mobile-snapshot")
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the offline Android snapshot to this SQLite file.",
)
def export_mobile_snapshot_command(output: Path | None) -> None:
    """Export the compact offline SQLite snapshot used by the Android app."""
    from us_picker.mobile_snapshot import (
        DEFAULT_MOBILE_SNAPSHOT_PATH,
        export_mobile_snapshot,
    )

    target_path = output or DEFAULT_MOBILE_SNAPSHOT_PATH
    exported_path = export_mobile_snapshot(target_path)
    console.print(
        f"[green]Mobile snapshot exported to[/green] [cyan]{exported_path}[/cyan]"
    )


@cli.command(name="export-mobile-feed")
@click.option(
    "--feed-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Write manifest.json and mobile_snapshot.db.gz into this directory.",
)
@click.option(
    "--base-download-url",
    type=str,
    default=None,
    help="Optional base URL prepended to the published snapshot filename in manifest.json.",
)
def export_mobile_feed_command(
    feed_dir: Path | None,
    base_download_url: str | None,
) -> None:
    """Export the cloud mobile feed used by the Android auto-sync flow."""
    from us_picker.mobile_feed import DEFAULT_FEED_DIRECTORY, export_mobile_feed

    target_dir = feed_dir or DEFAULT_FEED_DIRECTORY
    result = export_mobile_feed(
        target_dir,
        base_download_url=base_download_url,
    )

    # --- ARCHIVE AND CLEANUP DB TO PREVENT 100MB GITHUB LIMIT ---
    import os as os_mod
    import subprocess
    import sqlite3
    import csv
    import gzip
    from datetime import datetime, timedelta

    state_dir = "state-repo"
    db_path = "data/us_picker.db"
    
    # The runtime DB is the reproducible source for long-horizon backtests.
    # The exported mobile snapshot is already compact, so feed publication must
    # never prune scores or prices from this source database.  Keep the legacy
    # archive implementation unreachable until it can operate on a copy.
    archive_source_db = False
    if (
        archive_source_db
        and os_mod.path.exists(state_dir)
        and os_mod.path.exists(db_path)
    ):
        console.print("[dim]Archiving old data to prevent exceeding 100MB GitHub limit...[/dim]")
        try:
            cutoff = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
            archive_dir = os_mod.path.join(state_dir, "state", "current", "archive")
            os_mod.makedirs(archive_dir, exist_ok=True)
            archive_path = os_mod.path.join(archive_dir, "scoring_results_archive.csv.gz")
            
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM scoring_results WHERE scoring_date < ?",
                (cutoff,),
            )
            rows = cur.fetchall()
            
            if rows:
                with gzip.open(archive_path, 'wt', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([desc[0] for desc in cur.description])
                    writer.writerows(rows)
                
                conn.execute(
                    "DELETE FROM scoring_results WHERE scoring_date < ?",
                    (cutoff,),
                )
                
                # Clean daily_prices older than 2 years (Mobile snapshot only needs 730 days)
                cutoff_2y = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
                conn.execute(
                    "DELETE FROM daily_prices WHERE date < ?",
                    (cutoff_2y,),
                )
                conn.commit()
                conn.execute("VACUUM")
                console.print(f"[green]Archived {len(rows)} old scoring rows and vacuumed DB.[/green]")
                
                # Write a README notice
                readme_path = os_mod.path.join(state_dir, "state", "current", "ARCHIVE_NOTICE.md")
                with open(readme_path, "w") as f:
                    f.write(f"## Veri Arşivi\n100 MB GitHub limitini aşmamak için {cutoff} öncesi `scoring_results` verileri `archive/` klasörüne yedeklenmiştir.\nPortföy ve backtest performansı etkilenmemiştir, sadece eski ham model detayları arşivlenmiştir.\n")
                
                # Add to git
                subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=state_dir, check=False)
                subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=state_dir, check=False)
                subprocess.run(["git", "add", "state/current/archive/scoring_results_archive.csv.gz", "state/current/ARCHIVE_NOTICE.md"], cwd=state_dir, check=False)
                
        except Exception as e:
            console.print(f"[red]Error during DB cleanup: {e}[/red]")
        finally:
            if 'conn' in locals():
                conn.close()

    console.print(
        "[green]Mobile feed exported:[/green] "
        f"[cyan]{result.manifest_path}[/cyan] "
        f"and [cyan]{result.snapshot_path}[/cyan]"
    )


@cli.command(name="macro-check")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON report (used by GitHub Actions to build issues).",
)
@click.pass_context
def macro_check_command(ctx: click.Context, as_json: bool) -> None:
    """Report macro.yaml fields that are past their ``stale_after_days`` window.

    Exits with code 1 when any field is stale (GitHub Actions uses this
    to decide whether to open a refresh-reminder issue), 0 otherwise.
    """
    from us_picker.macro_check import _format_human, check_macro_staleness

    report = check_macro_staleness()
    if as_json:
        click.echo(report.to_json())
    else:
        console.print(_format_human(report))

    if report.is_stale:
        ctx.exit(1)


@cli.command(name="monitor-alerts")
@click.pass_context
def monitor_alerts(ctx: click.Context) -> None:
    """Monitor portfolio for price triggers and KAP disclosures, sending Telegram alerts."""
    from us_picker.notifications.monitor_alerts import monitor_portfolio
    console.print("[bold cyan]Starting portfolio and KAP monitor...[/bold cyan]")
    try:
        monitor_portfolio()
        console.print("[bold green]Monitoring cycle completed successfully.[/bold green]")
    except Exception as e:
        raise click.ClickException(f"Monitoring failed: {e}") from e


@cli.command(name="export-live-prices")
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=Path("live_prices.json"),
    show_default=True,
    help="Output JSON path.",
)
@click.option(
    "--tickers-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Published compact live_tickers.json; avoids restoring the full DB.",
)
def export_live_prices(output: Path, tickers_file: Path | None) -> None:
    """Export the active BIST universe's near-live Yahoo price feed."""
    from us_picker.live_prices import load_live_tickers, write_live_price_feed

    try:
        tickers = load_live_tickers(tickers_file) if tickers_file else None
        payload = write_live_price_feed(output, tickers=tickers)
    except Exception as exc:
        raise click.ClickException(f"Live price export failed: {exc}") from exc

    console.print(
        "[bold green]Live price feed exported:[/bold green] "
        f"{payload['success_count']}/{payload['requested_count']} quotes -> {output}"
    )


@cli.command(name="cash-status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON report (used by GitHub Actions to build issues).",
)
@click.option(
    "--compute",
    is_flag=True,
    default=False,
    help=(
        "Force a fresh compute for today using the live regime classifiers "
        "and persist the row. Without this flag the command only reads the "
        "most recently persisted state."
    ),
)
@click.pass_context
def cash_status_command(ctx: click.Context, as_json: bool, compute: bool) -> None:
    """Report the current Phase 4 cash-allocation state.

    Exits with code 1 when the state transitioned on the most recent row
    (GitHub Actions uses this to decide whether to open a state-change
    tracking issue).
    """
    import json as _json
    from datetime import date as _date

    from us_picker import read_service
    from us_picker.db.connection import ensure_runtime_db_ready, session_scope
    from us_picker.portfolio.cash_signal import (
        CashSignalCalculator,
        CashSignalConfig,
        next_possible_transition_date,
    )
    from us_picker.db.schema import CashAllocationState

    ensure_runtime_db_ready()

    cfg = CashSignalConfig.load()

    if compute:
        with session_scope() as session:
            result = CashSignalCalculator(cfg).compute(session, _date.today())
        state = {
            "date": result.date,
            "state": result.state,
            "cash_pct": result.cash_pct,
            "target_state": result.target_state,
            "market_regime": result.market_regime,
            "macro_regime": result.macro_regime,
            "raw_signal": result.raw_signal,
            "days_in_state": result.days_in_state,
            "last_transition_date": result.last_transition_date,
            "transitioned_today": result.transitioned_today,
            "notes": result.notes,
        }
    else:
        state = read_service.get_latest_cash_state()

    if state is None:
        if as_json:
            click.echo(_json.dumps({"state": None, "message": "no cash state persisted yet"}))
        else:
            console.print("[yellow]No cash state persisted yet. Run `pick` or pass --compute.[/yellow]")
        return

    # Compute the earliest date the cooldown will release another transition.
    next_tx: _date | None = None
    if state.get("last_transition_date"):
        with session_scope() as session:
            row = (
                session.query(CashAllocationState)
                .order_by(CashAllocationState.date.desc())
                .first()
            )
            if row is not None:
                next_tx = next_possible_transition_date(row, cfg)

    if as_json:
        payload = {
            **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in state.items()},
            "enabled": cfg.enabled,
            "next_possible_transition_date": next_tx.isoformat() if next_tx else None,
        }
        click.echo(_json.dumps(payload))
    else:
        console.print(f"[bold]Cash allocation state[/bold] as of {state['date']}")
        console.print(f"  state:            [cyan]{state['state']}[/cyan]  (cash_pct={state['cash_pct']:.0%})")
        console.print(f"  target (raw):     {state['target_state']}  (market={state['market_regime']}, macro={state['macro_regime']}, raw={state['raw_signal']})")
        console.print(f"  days in state:    {state['days_in_state']}")
        if state.get("last_transition_date"):
            console.print(f"  last transition:  {state['last_transition_date']}")
        if next_tx is not None:
            console.print(f"  next allowed:     {next_tx}")
        if state.get("notes"):
            console.print(f"  notes:            {state['notes']}")
        if not cfg.enabled:
            console.print("  [yellow]kill-switch is engaged: state forced to NORMAL[/yellow]")

    if state.get("transitioned_today"):
        ctx.exit(1)


def _mask_secret(value: str) -> str:
    """Mask secrets for display without exposing full value."""
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 5)}{value[-2:]}"


def _parse_tickers(raw: str) -> tuple[str, ...]:
    """Parse comma-separated tickers into a unique uppercase tuple."""
    cleaned = [t.strip().upper() for t in raw.split(",") if t.strip()]
    # Preserve order while removing duplicates.
    unique = list(dict.fromkeys(cleaned))
    return tuple(unique)


def _prompt_portfolio(default: str = "all") -> str:
    """Prompt for portfolio selection."""
    return click.prompt(
        "Portfolio",
        type=click.Choice(["alpha", "all"], case_sensitive=False),
        default=default,
        show_default=True,
    ).lower()


def _prompt_api_key() -> str:
    """Prompt for TCMB API key using a terminal-compatible visible input."""
    console.print(
        "[dim]Enter/paste TCMB EVDS API key and press Enter "
        "(input is visible in terminal).[/dim]"
    )
    try:
        return click.prompt("TCMB API key", type=str).strip()
    except (click.Abort, EOFError):
        return ""


def _ensure_tcmb_api_key() -> None:
    """Prompt user for TCMB API key if missing in current session."""
    key = os.environ.get("TCMB_API_KEY", "").strip()
    if key:
        console.print(
            f"[green]TCMB_API_KEY is set for this session:[/green] "
            f"[cyan]{_mask_secret(key)}[/cyan]"
        )
        return

    console.print(
        "[yellow]TCMB_API_KEY is not set. TCMB macro data may be unavailable.[/yellow]"
    )
    if click.confirm("Set TCMB API key now for this terminal session?", default=True):
        attempts = 0
        while attempts < 3:
            new_key = _prompt_api_key()
            if new_key:
                os.environ["TCMB_API_KEY"] = new_key
                console.print("[green]TCMB_API_KEY set for current session.[/green]")
                return
            attempts += 1
            console.print("[yellow]Empty key entered.[/yellow]")
            if attempts < 3 and not click.confirm("Try entering API key again?", default=True):
                break
        console.print("[yellow]Keeping TCMB_API_KEY unset for now.[/yellow]")


def _run_full_pipeline_for_tickers(
    ctx: click.Context, tickers: tuple[str, ...],
) -> None:
    """Run full pipeline with optional ticker filter."""
    ctx.invoke(run, ticker=tickers)


def _stage_menu(ctx: click.Context) -> None:
    """Interactive stage-by-stage menu."""
    while True:
        console.print("\n[bold]Stage Menu[/bold]")
        console.print("  1) Fetch (all)")
        console.print("  2) Fetch (selected tickers)")
        console.print("  3) Clean")
        console.print("  4) Score")
        console.print("  5) Pick")
        console.print("  6) Report (terminal)")
        console.print("  7) Report (excel)")
        console.print("  8) Historical Backfill (5yr quarterly financials)")
        console.print("  9) Run Backtest (multi-year)")
        console.print("  0) Back")

        choice = click.prompt(
            "Select option",
            type=click.Choice(["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]),
        )

        if choice == "0":
            return
        if choice == "1":
            ctx.invoke(
                fetch,
                ticker=(),
                prices_only=False,
                refresh_universe=False,
                price_days=None,
                limit=0,
                history=False,
            )
        elif choice == "2":
            raw = click.prompt("Enter tickers (comma-separated, e.g. THYAO,BIMAS)")
            tickers = _parse_tickers(raw)
            if not tickers:
                console.print("[yellow]No valid tickers entered.[/yellow]")
                continue
            ctx.invoke(
                fetch,
                ticker=tickers,
                prices_only=False,
                refresh_universe=False,
                price_days=None,
                limit=0,
                history=False,
            )
        elif choice == "3":
            ctx.invoke(clean)
        elif choice == "4":
            ctx.invoke(score)
        elif choice == "5":
            force = click.confirm("Force rotation even if not Monday?", default=False)
            ctx.invoke(pick, force=force)
        elif choice == "6":
            portfolio = _prompt_portfolio(default="all")
            ctx.invoke(report, portfolio=portfolio, format="terminal")
        elif choice == "7":
            portfolio = _prompt_portfolio(default="all")
            ctx.invoke(report, portfolio=portfolio, format="excel")
        elif choice == "8":
            console.print(
                "\n[bold]This fetches 5 years of quarterly financial data "
                "from IsYatirim.[/bold]"
            )
            console.print("[dim]This is a one-time operation — historical data doesn't change.[/dim]")
            if click.confirm("Proceed with historical backfill?", default=True):
                ctx.invoke(
                    fetch,
                    ticker=(),
                    prices_only=False,
                    refresh_universe=False,
                    price_days=None,
                    limit=0,
                    history=True,
                )
        elif choice == "9":
            start = click.prompt("Start date (YYYY-MM-DD)", default="2023-01-01")
            ctx.invoke(backtest, start_date=start)


def _daily_ops_menu(ctx: click.Context) -> None:
    """Interactive daily operations menu."""
    while True:
        console.print("\n[bold]Daily Operations[/bold]")
        console.print("  1) Status")
        console.print("  2) Inspect ticker")
        console.print("  3) Check exits (and apply)")
        console.print("  4) Push to Google Sheets")
        console.print("  5) Cash Allocation Status")
        console.print("  0) Back")

        choice = click.prompt(
            "Select option",
            type=click.Choice(["1", "2", "3", "4", "5", "0"]),
        )

        if choice == "0":
            return
        if choice == "1":
            ctx.invoke(status)
        elif choice == "2":
            ticker = click.prompt("Ticker").strip().upper()
            if not ticker:
                console.print("[yellow]Ticker cannot be empty.[/yellow]")
                continue
            ctx.invoke(inspect, ticker=ticker)
        elif choice == "3":
            apply = click.confirm("Apply detected exit signals to database?", default=False)
            ctx.invoke(check_exits, apply=apply)
        elif choice == "4":
            portfolio = _prompt_portfolio(default="all")
            sheet_name = click.prompt("Google Sheet name", default="BIST Portfolio Tracker")
            ctx.invoke(push_sheets, portfolio=portfolio, sheet_name=sheet_name)
        elif choice == "5":
            ctx.invoke(cash_status_command)


def _setup_menu() -> None:
    """Interactive setup/config menu."""
    from pathlib import Path
    from us_picker.db.connection import get_engine

    while True:
        console.print("\n[bold]Setup and Configuration[/bold]")
        console.print("  1) Set/replace TCMB API key (session only)")
        console.print("  2) Clear TCMB API key (session)")
        console.print("  3) Show setup status")
        console.print("  4) Show command to persist API key")
        console.print("  0) Back")

        choice = click.prompt(
            "Select option",
            type=click.Choice(["1", "2", "3", "4", "0"]),
        )

        if choice == "0":
            return
        if choice == "1":
            new_key = _prompt_api_key()
            if not new_key:
                console.print("[yellow]Empty key entered. No change made.[/yellow]")
                continue
            os.environ["TCMB_API_KEY"] = new_key
            console.print("[green]TCMB_API_KEY updated for current session.[/green]")
        elif choice == "2":
            os.environ.pop("TCMB_API_KEY", None)
            console.print("[yellow]TCMB_API_KEY removed from current session.[/yellow]")
        elif choice == "3":
            key = os.environ.get("TCMB_API_KEY", "").strip()
            root_creds = Path("service_account.json").exists()
            cfg_creds = Path("config/service_account.json").exists()
            engine = get_engine()
            console.print(f"TCMB_API_KEY: [cyan]{_mask_secret(key)}[/cyan]")
            console.print(
                "Google creds file: "
                f"[cyan]root={root_creds}, config={cfg_creds}[/cyan]"
            )
            console.print(f"DB path: [cyan]{engine.url}[/cyan]")
        elif choice == "4":
            console.print(
                "PowerShell (persist): [cyan]setx TCMB_API_KEY \"YOUR_EVDS_KEY\"[/cyan]"
            )
            console.print(
                "After running setx, open a new terminal for it to take effect."
            )


@cli.command()
@click.pass_context
def menu(ctx: click.Context) -> None:
    """Interactive menu for setup, pipeline runs, and daily operations."""
    console.print("[bold blue]BIST Stock Picker - Interactive Menu[/bold blue]")
    if ctx.obj.get("dry_run", False):
        console.print("[yellow]Global --dry-run is active.[/yellow]")

    _ensure_tcmb_api_key()

    while True:
        console.print("\n[bold]Main Menu[/bold]")
        console.print("  1) Quick test run (5 tickers)")
        console.print("  2) Full run (all stocks)")
        console.print("  3) Full run (selected tickers)")
        console.print("  4) Stage-by-stage tools")
        console.print("  5) Daily operations")
        console.print("  6) Setup and configuration")
        console.print("  0) Exit")

        choice = click.prompt(
            "Select option",
            type=click.Choice(["1", "2", "3", "4", "5", "6", "0"]),
        )

        try:
            if choice == "0":
                console.print("[green]Exiting menu.[/green]")
                return
            if choice == "1":
                tickers = ("THYAO", "BIMAS", "GARAN", "SAHOL", "ASELS")
                if click.confirm(
                    f"Run full pipeline for test tickers: {', '.join(tickers)}?",
                    default=True,
                ):
                    _run_full_pipeline_for_tickers(ctx, tickers)
            elif choice == "2":
                if click.confirm(
                    "Run full pipeline for all stocks? This may take a long time.",
                    default=False,
                ):
                    _run_full_pipeline_for_tickers(ctx, ())
            elif choice == "3":
                raw = click.prompt("Enter tickers (comma-separated, e.g. THYAO,BIMAS)")
                tickers = _parse_tickers(raw)
                if not tickers:
                    console.print("[yellow]No valid tickers entered.[/yellow]")
                    continue
                if click.confirm(
                    f"Run full pipeline for: {', '.join(tickers)}?",
                    default=True,
                ):
                    _run_full_pipeline_for_tickers(ctx, tickers)
            elif choice == "4":
                _stage_menu(ctx)
            elif choice == "5":
                _daily_ops_menu(ctx)
            elif choice == "6":
                _setup_menu()
        except Exception as exc:
            console.print(f"[red]Operation failed:[/red] {exc}")


if __name__ == "__main__":
    cli()
