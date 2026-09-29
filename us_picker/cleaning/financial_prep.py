"""Financial metrics calculator for BIST Stock Picker.

Calculates adjusted financial metrics from raw financial statements stored
in the database. Handles IAS 29 monetary gain/loss stripping, owner earnings,
free cash flow, adjusted ROE/ROA/EPS, and real (inflation-adjusted) EPS growth.

Critical rules:
- For BANKS: do NOT strip monetary gain/loss
- Missing data -> set metrics to None, not 0
- Defensive parsing: try multiple item codes and label patterns
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from sqlalchemy.orm import Session

from us_picker.cleaning.inflation import InflationAdjuster, _find_item_by_codes
from us_picker.cleaning.financial_periods import (
    analytical_input_hash,
    analytical_source_periods_json,
    analytical_statement_from_map,
    latest_statement_map,
    parse_statement_items,
)
from us_picker.db.schema import (
    AdjustedMetric,
    Company,
    CpiHistory,
    FinancialStatement,
    MacroRegime,
    ScoringResult,
)
from us_picker.scoring.context import estimated_metric_visibility_date

logger = logging.getLogger("us_picker.cleaning.financial_prep")

# ---- Item code constants (verified from real Is Yatirim data) ----

# Income statement
CODES_NET_INCOME_PARENT = ["3Z"]      # Parent Shares
CODES_NET_INCOME_TOTAL = ["3L"]       # NET PROFIT AFTER TAXES
CODES_GROSS_PROFIT = ["3D"]           # GROSS PROFIT (LOSS)
CODES_OPERATING_PROFIT = ["3DF"]      # OPERATING PROFITS
CODES_PRE_TAX_PROFIT = ["3I"]         # PROFIT BEFORE TAX
CODES_NET_SALES = ["3C"]              # Net Sales

# Supplementary (stored with income data, 4B* prefix)
CODES_DA_INCOME = ["4B"]              # Depreciation & Amortization (income supp.)

# Balance sheet
CODES_TOTAL_ASSETS = ["1BL", "1Z"]    # TOTAL ASSETS (including insurance 1Z)
CODES_CURRENT_ASSETS = ["1A"]         # CURRENT ASSETS
CODES_CURRENT_LIABILITIES = ["2A"]    # SHORT TERM LIABILITIES
CODES_TOTAL_EQUITY = ["2N", "2O"]     # SHAREHOLDERS EQUITY (including insurance 2O)
CODES_PARENT_EQUITY = ["2O"]          # Parent Shareholders Capital
CODES_SHARE_CAPITAL = ["2OA", "2MEA", "2MEB"] # Share Capital (including insurance 2MEA, 2MEB)
CODES_PP_AND_E = ["1BC"]              # Property, Plant & Equipment

# Cash flow statement
CODES_CFO = ["4C"]                    # Net Cash from Operations
CODES_DA_CASHFLOW = ["4CAB"]          # Depreciation & Amortisation (cashflow)
CODES_CAPEX = ["4CAI"]                # Capital Expenditures (CapEx) - typically negative
CODES_FCF = ["4CB"]                   # Free Cash Flow (reported)
CODES_WC_CHANGE = ["4CAF"]            # Change in Working Capital

# Income statement — deferred tax
CODES_DEFERRED_TAX = ["3HA"]          # Deferred tax income/expense

# Fallback maintenance capex ratio (when insufficient history for Greenwald)
MAINTENANCE_CAPEX_RATIO = 0.70


def _estimate_maintenance_capex(
    ppe_sales_history: list[tuple[float, float]],
    current_sales_growth: float | None,
    total_capex: float,
) -> float:
    """Bruce Greenwald method: split Total CapEx into Maintenance vs Growth.

    Growth CapEx = avg(PP&E / Sales) × ΔSales.
    Maintenance CapEx = Total CapEx - Growth CapEx.

    Falls back to 70% of Total CapEx if fewer than 3 years of data.
    """
    if len(ppe_sales_history) < 3 or current_sales_growth is None:
        return total_capex * MAINTENANCE_CAPEX_RATIO  # fallback

    ratios = [
        ppe / sales
        for ppe, sales in ppe_sales_history
        if sales is not None and sales > 0 and ppe is not None and ppe > 0
    ]
    if len(ratios) < 2:
        return total_capex * MAINTENANCE_CAPEX_RATIO

    avg_ratio = sum(ratios) / len(ratios)
    growth_capex = avg_ratio * max(0.0, current_sales_growth)
    maintenance = total_capex - growth_capex

    # Floor: maintenance can't be negative or exceed total capex
    return max(0.0, min(total_capex, maintenance))


def _estimate_excess_depreciation(
    da_sales_history: list[tuple[float, float]],
    current_da: float,
    current_sales: float,
) -> float:
    """Estimate excess depreciation caused by IAS 29 asset revaluation.

    If current D&A/Sales is more than 1.5× the historical median, the excess
    is likely inflation-driven and should be added back to owner earnings.
    Returns 0.0 when there is no detectable excess.
    """
    if len(da_sales_history) < 3 or current_sales <= 0 or current_da <= 0:
        return 0.0

    historical_ratios = [
        da / sales
        for da, sales in da_sales_history
        if sales is not None and sales > 0 and da is not None and da > 0
    ]
    if len(historical_ratios) < 2:
        return 0.0

    historical_ratios.sort()
    median_ratio = historical_ratios[len(historical_ratios) // 2]
    current_ratio = current_da / current_sales

    # If current D&A ratio is >1.5x of historical median, excess is inflationary
    if current_ratio > median_ratio * 1.5 and median_ratio > 0:
        expected_da = median_ratio * current_sales
        excess = current_da - expected_da
        logger.debug(
            "Excess D&A detected: current ratio %.3f vs median %.3f, excess=%.0f",
            current_ratio, median_ratio, excess,
        )
        return max(0.0, excess)

    return 0.0


class MetricsCalculator:
    """Calculates adjusted financial metrics from raw financial statements.

    Pulls financial statement JSON from the DB, applies IAS 29 adjustments,
    and stores results in the adjusted_metrics table.

    Args:
        session: SQLAlchemy session for DB access.
        console: Rich console for output. Optional.
    """

    def __init__(
        self, session: Session, console: Optional[Console] = None
    ) -> None:
        self._session = session
        self._console = console or Console()
        self._adjuster = InflationAdjuster()
        self._cpi_series: Optional[pd.Series] = None

    def calculate_adjusted_metrics(self, company_id: int) -> int:
        """Calculate and store adjusted metrics for a single company.

        Processes annual and complete interim financial statements for the
        company. Interim cumulative flows are converted to TTM before metrics
        are calculated; incomplete TTM source triplets are skipped rather than
        pretending a quarter is a full year.

        Args:
            company_id: Database ID of the company.

        Returns:
            Number of metric rows upserted.
        """
        company = self._session.get(Company, company_id)
        if company is None:
            logger.warning("Company ID %d not found", company_id)
            return 0

        is_bank = (company.company_type or "").upper() == "BANK"

        # Preload/deduplicate once. The previous annual implementation issued
        # three SQL queries per period; quarterly TTM would multiply that cost.
        statements = (
            self._session.query(FinancialStatement)
            .filter(FinancialStatement.company_id == company_id)
            .order_by(
                FinancialStatement.period_end,
                FinancialStatement.statement_type,
                FinancialStatement.version,
            )
            .all()
        )
        statement_map = latest_statement_map(statements)
        period_dates = sorted(
            period_end
            for (period_end, statement_type) in statement_map
            if statement_type == "INCOME"
        )

        if not period_dates:
            logger.debug("No income statements for company %d", company_id)
            return 0

        upserted = 0
        stale_score_cache_from: Optional[date] = None
        eps_by_period: dict[tuple[int, int], float] = {}
        equity_by_period: dict[tuple[int, int], float] = {}
        assets_by_period: dict[tuple[int, int], float] = {}
        sales_by_period: dict[tuple[int, int], float] = {}

        # Accumulators for Greenwald CapEx and excess D&A detection
        ppe_sales_history: list[tuple[float, float]] = []
        da_sales_history: list[tuple[float, float]] = []

        for period_end in period_dates:
            income_statement = analytical_statement_from_map(
                statement_map, company_id, period_end, "INCOME"
            )
            balance_statement = analytical_statement_from_map(
                statement_map, company_id, period_end, "BALANCE"
            )
            cashflow_statement = analytical_statement_from_map(
                statement_map, company_id, period_end, "CASHFLOW"
            )
            income_data = parse_statement_items(income_statement)
            balance_data = parse_statement_items(balance_statement)
            cashflow_data = parse_statement_items(cashflow_statement)

            if not income_data:
                logger.debug(
                    "Skipping period %s for company %d: incomplete annual/TTM income sources",
                    period_end,
                    company_id,
                )
                continue

            # Extract fields
            income_fields = _extract_income_fields(income_data)

            # Skip periods where all income fields are None — this happens when
            # IsYatirim returns a placeholder statement for a period not yet
            # filed (e.g. 2025/12 in February 2026, before the annual report
            # deadline).  Using an empty statement would reset prev_eps to None
            # and produce 0-composite scores for all scorers.
            key_income_fields = [
                income_fields.get("net_income_parent"),
                income_fields.get("net_income_total"),
                income_fields.get("net_sales"),
                income_fields.get("operating_profit"),
            ]
            if all(v is None for v in key_income_fields):
                logger.debug(
                    "Skipping empty period %s for company %d (all key income fields None)",
                    period_end, company_id,
                )
                continue

            balance_fields = _extract_balance_fields(balance_data) if balance_data else {}
            cashflow_fields = _extract_cashflow_fields(cashflow_data) if cashflow_data else {}

            # Get reported net income
            reported_ni = income_fields.get("net_income_parent") or income_fields.get("net_income_total")

            # Strip monetary gain/loss (skip for banks)
            if is_bank:
                adjusted_ni = reported_ni
                monetary_gl = 0.0
            else:
                adjusted_ni, monetary_gl = self._adjuster.strip_monetary_gain_loss(
                    income_data
                )

            # ── Deferred tax stripping (non-cash IAS 29 artifact) ─────────
            deferred_tax_val = income_fields.get("deferred_tax")
            deferred_tax_stripped: Optional[float] = None
            if (
                not is_bank
                and deferred_tax_val is not None
                and deferred_tax_val > 0
                and adjusted_ni is not None
            ):
                # Positive deferred_tax = income (non-cash); strip it
                deferred_tax_stripped = deferred_tax_val
                adjusted_ni = adjusted_ni - deferred_tax_val

            # D&A: prefer cashflow version, fallback to income supplementary
            da = cashflow_fields.get("da") or income_fields.get("da")

            # CapEx (typically negative from cashflow)
            capex = cashflow_fields.get("capex")
            capex_abs = abs(capex) if capex is not None else None

            # Working capital change
            wc_change = cashflow_fields.get("wc_change")

            # CFO
            cfo = cashflow_fields.get("cfo")

            # Balance sheet items
            total_equity = balance_fields.get("total_equity")
            total_assets = balance_fields.get("total_assets")
            share_capital = balance_fields.get("share_capital")
            ppe = balance_fields.get("ppe")
            net_sales = income_fields.get("net_sales")

            # Annual and TTM rates compare with the same reporting point one
            # year earlier — never the previous quarter.
            comparable_key = (period_end.year - 1, period_end.month)
            prev_eps = eps_by_period.get(comparable_key)
            prev_equity = equity_by_period.get(comparable_key)
            prev_assets = assets_by_period.get(comparable_key)
            prev_sales = sales_by_period.get(comparable_key)
            prev_period_end = (
                date(period_end.year - 1, period_end.month, period_end.day)
                if prev_eps is not None
                else None
            )

            # Sales growth for Greenwald method
            current_sales_growth = None
            if net_sales is not None and prev_sales is not None:
                current_sales_growth = net_sales - prev_sales

            # Calculate shares outstanding from share capital (par value = 1 TRY)
            shares_outstanding = share_capital  # 1 TRY par value in Turkey

            # ── Greenwald Maintenance CapEx decomposition ─────────────────
            maint_capex = 0.0
            growth_capex_val: Optional[float] = None
            if capex_abs is not None and capex_abs > 0:
                maint_capex = _estimate_maintenance_capex(
                    ppe_sales_history, current_sales_growth, capex_abs
                )
                growth_capex_val = capex_abs - maint_capex

            # ── IAS 29 excess depreciation detection ──────────────────────
            excess_da = 0.0
            if da is not None and net_sales is not None and not is_bank:
                excess_da = _estimate_excess_depreciation(
                    da_sales_history, da, net_sales
                )

            # ── Owner Earnings ────────────────────────────────────────────
            # OE = Adj NI + D&A + excess_da_addback - Maintenance CapEx - ΔWC
            owner_earnings = None
            if adjusted_ni is not None and da is not None:
                delta_wc = wc_change if wc_change is not None else 0.0
                owner_earnings = adjusted_ni + da + excess_da - maint_capex - delta_wc

            # Free Cash Flow = CFO - Total CapEx
            free_cash_flow = None
            if cfo is not None and capex_abs is not None:
                free_cash_flow = cfo - capex_abs

            # ROE adjusted = adj NI / AVERAGE equity (beginning + ending / 2)
            roe_adjusted = None
            if adjusted_ni is not None and total_equity and total_equity != 0:
                avg_equity = (total_equity + prev_equity) / 2.0 if prev_equity else total_equity
                roe_adjusted = adjusted_ni / avg_equity

            # ROA adjusted = adj NI / AVERAGE total assets
            roa_adjusted = None
            if adjusted_ni is not None and total_assets and total_assets != 0:
                avg_assets = (total_assets + prev_assets) / 2.0 if prev_assets else total_assets
                roa_adjusted = adjusted_ni / avg_assets

            # EPS adjusted = adj NI / shares outstanding
            eps_adjusted = None
            if adjusted_ni is not None and shares_outstanding and shares_outstanding != 0:
                eps_adjusted = adjusted_ni / shares_outstanding

            # Real EPS growth (inflation-adjusted YoY)
            real_eps_growth = None
            if eps_adjusted is not None and prev_eps is not None and prev_period_end is not None:
                cpi = self._get_cpi_series()
                real_eps_growth = self._adjuster.calculate_real_growth(
                    eps_adjusted, prev_eps, period_end, prev_period_end, cpi
                )

            # Real ROE / ROA via Fisher (audit CRITICAL #3, 2026-05-07).
            # Trailing-12m CPI ending at period_end. Banks correctly skip
            # IAS-29 strip but the Fisher conversion still applies — banks
            # use `models/banking.py` which doesn't read these columns.
            cpi_for_real = self._get_cpi_series()
            roe_real = self._adjuster.deflate_rate(
                roe_adjusted, period_end, cpi_for_real
            )
            roa_real = self._adjuster.deflate_rate(
                roa_adjusted, period_end, cpi_for_real
            )

            # The metric becomes knowable only when every source behind the
            # annual/TTM analytical row was known.
            analytical_statements = (
                income_statement,
                balance_statement,
                cashflow_statement,
            )
            publication_dates = [
                statement.publication_date
                for statement in analytical_statements
                if statement is not None and statement.publication_date is not None
            ]
            publication_date = max(publication_dates) if publication_dates else None
            source_period_type = income_statement.period_type
            calculation_basis = income_statement.calculation_basis
            source_periods_json = analytical_source_periods_json(analytical_statements)
            input_hash = analytical_input_hash(analytical_statements)

            # ── Update accumulators for next iteration ────────────────────
            current_key = (period_end.year, period_end.month)
            if eps_adjusted is not None:
                eps_by_period[current_key] = eps_adjusted
            if total_equity is not None:
                equity_by_period[current_key] = total_equity
            if total_assets is not None:
                assets_by_period[current_key] = total_assets
            if net_sales is not None:
                sales_by_period[current_key] = net_sales

            # Long-run Greenwald/excess-D&A baselines stay annual; adding four
            # overlapping TTM rows per year would overweight recent history.
            if source_period_type == "ANNUAL":
                if ppe is not None and net_sales is not None:
                    ppe_sales_history.append((ppe, net_sales))
                if da is not None and net_sales is not None:
                    da_sales_history.append((da, net_sales))

            # Upsert into adjusted_metrics
            existing = (
                self._session.query(AdjustedMetric)
                .filter(
                    AdjustedMetric.company_id == company_id,
                    AdjustedMetric.period_end == period_end,
                )
                .first()
            )

            metric_values = {
                "source_period_type": source_period_type,
                "calculation_basis": calculation_basis,
                "source_periods_json": source_periods_json,
                "input_hash": input_hash,
                "reported_net_income": reported_ni,
                "monetary_gain_loss": monetary_gl,
                "adjusted_net_income": adjusted_ni,
                "owner_earnings": owner_earnings,
                "free_cash_flow": free_cash_flow,
                "roe_adjusted": roe_adjusted,
                "roa_adjusted": roa_adjusted,
                "roe_real": roe_real,
                "roa_real": roa_real,
                "eps_adjusted": eps_adjusted,
                "real_eps_growth_pct": real_eps_growth,
                "maintenance_capex": maint_capex if capex_abs else None,
                "growth_capex": growth_capex_val,
                "deferred_tax_stripped": deferred_tax_stripped,
                "excess_depreciation_addback": (
                    excess_da if excess_da > 0 else None
                ),
                "publication_date": publication_date,
            }

            if existing:
                input_changed = any(
                    getattr(existing, field) != value
                    for field, value in metric_values.items()
                )
                for field, value in metric_values.items():
                    setattr(existing, field, value)
            else:
                input_changed = True
                metric = AdjustedMetric(
                    company_id=company_id,
                    period_end=period_end,
                    **metric_values,
                )
                self._session.add(metric)

            if input_changed:
                visible_from = publication_date or estimated_metric_visibility_date(
                    period_end
                )
                if (
                    stale_score_cache_from is None
                    or visible_from < stale_score_cache_from
                ):
                    stale_score_cache_from = visible_from

            upserted += 1

        if upserted > 0:
            self._session.flush()

        # A factor input changed without a code-version change. Mark every
        # affected cross-sectional cache date stale so the backtest rebuilds
        # the whole date (normalization/ranks make a one-company patch unsafe).
        if stale_score_cache_from is not None:
            invalidated = (
                self._session.query(ScoringResult)
                .filter(
                    ScoringResult.company_id == company_id,
                    ScoringResult.scoring_date >= stale_score_cache_from,
                    ScoringResult.pipeline_version.isnot(None),
                )
                .update(
                    {ScoringResult.pipeline_version: None},
                    synchronize_session=False,
                )
            )
            if invalidated:
                logger.info(
                    "Invalidated %d score-cache row(s) for company %d from %s",
                    invalidated,
                    company_id,
                    stale_score_cache_from,
                )

        return upserted

    def calculate_all(self) -> dict:
        """Calculate adjusted metrics for all active companies with financials.

        Returns:
            Stats dict: {total, calculated, skipped, errors}.
        """
        # Get all active companies that have at least one financial statement
        companies = (
            self._session.query(Company)
            .filter(Company.is_active.is_(True))
            .join(FinancialStatement)
            .distinct()
            .all()
        )

        stats = {"total": len(companies), "calculated": 0, "skipped": 0, "errors": 0}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self._console,
        ) as progress:
            task = progress.add_task("Calculating metrics", total=len(companies))

            for company in companies:
                progress.update(task, description=f"Metrics: {company.ticker}")
                try:
                    count = self.calculate_adjusted_metrics(company.id)
                    if count > 0:
                        stats["calculated"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception:
                    logger.exception("Error calculating metrics for %s", company.ticker)
                    stats["errors"] += 1
                finally:
                    progress.advance(task)

        self._console.print(
            f"[green]Metrics calculated:[/green] {stats['calculated']} companies, "
            f"{stats['skipped']} skipped, {stats['errors']} errors"
        )
        return stats

    def _get_cpi_series(self) -> Optional[pd.Series]:
        """Load CPI index level series for inflation adjustments.

        2026-04-30 audit fix: previously this loaded `macro_regime.cpi_yoy_pct`
        (a YoY rate, e.g. 0.65 = 65%) and fed it into
        `InflationAdjuster.calculate_real_growth`, which expects CPI index
        levels and computes `cpi_current / cpi_previous - 1` to derive
        period-over-period inflation. Feeding YoY scalars made
        `real_eps_growth_pct` meaningless. We now read TCMB TP.FG.J0 monthly
        index levels from the dedicated `cpi_history` table populated by
        `data.fetcher._upsert_cpi_history`.

        Falls back to the legacy MacroRegime YoY path if cpi_history is
        empty (e.g., a DB seeded before the fix has run a fetch). The
        legacy path keeps the previous (broken) behavior to avoid silent
        regressions; correctness recovers as soon as fetch_macro runs.

        Returns:
            pandas Series with DatetimeIndex and CPI index level values,
            or None if no CPI data is available at all.
        """
        if self._cpi_series is not None:
            return self._cpi_series

        rows = (
            self._session.query(CpiHistory.date, CpiHistory.cpi_index)
            .order_by(CpiHistory.date)
            .all()
        )

        if rows:
            dates = [r[0] for r in rows]
            values = [r[1] for r in rows]
            self._cpi_series = pd.Series(
                values, index=pd.DatetimeIndex(dates), name="cpi_index"
            )
            return self._cpi_series

        # Legacy fallback (cpi_history not populated yet). The
        # macro_regime.cpi_yoy_pct values are YoY rates not index levels,
        # so calculate_real_growth produces incorrect numbers from them;
        # this branch exists only to preserve pre-fix behavior on stale DBs.
        logger.warning(
            "cpi_history is empty; falling back to macro_regime.cpi_yoy_pct "
            "(this produces inaccurate real-growth values until fetch_macro "
            "populates cpi_history)"
        )
        rows = (
            self._session.query(MacroRegime.date, MacroRegime.cpi_yoy_pct)
            .filter(MacroRegime.cpi_yoy_pct.isnot(None))
            .order_by(MacroRegime.date)
            .all()
        )
        if not rows:
            logger.debug("No CPI data in cpi_history or macro_regime")
            return None

        dates = [r[0] for r in rows]
        values = [r[1] for r in rows]
        self._cpi_series = pd.Series(
            values, index=pd.DatetimeIndex(dates), name="cpi_yoy_pct"
        )
        return self._cpi_series


def _extract_income_fields(data: list[dict]) -> dict:
    """Extract key fields from income statement JSON.

    Args:
        data: Parsed income statement JSON items.

    Returns:
        Dict with extracted values (may contain None).
    """
    return {
        "net_income_parent": _find_item_by_codes(data, CODES_NET_INCOME_PARENT),
        "net_income_total": _find_item_by_codes(data, CODES_NET_INCOME_TOTAL),
        "gross_profit": _find_item_by_codes(data, CODES_GROSS_PROFIT),
        "operating_profit": _find_item_by_codes(data, CODES_OPERATING_PROFIT),
        "pre_tax_profit": _find_item_by_codes(data, CODES_PRE_TAX_PROFIT),
        "net_sales": _find_item_by_codes(data, CODES_NET_SALES),
        "da": _find_item_by_codes(data, CODES_DA_INCOME),
        "deferred_tax": _find_item_by_codes(data, CODES_DEFERRED_TAX),
    }


def _extract_balance_fields(data: list[dict]) -> dict:
    """Extract key fields from balance sheet JSON.

    Args:
        data: Parsed balance sheet JSON items.

    Returns:
        Dict with extracted values (may contain None).
    """
    return {
        "total_assets": _find_item_by_codes(data, CODES_TOTAL_ASSETS),
        "current_assets": _find_item_by_codes(data, CODES_CURRENT_ASSETS),
        "current_liabilities": _find_item_by_codes(data, CODES_CURRENT_LIABILITIES),
        "total_equity": _find_item_by_codes(data, CODES_TOTAL_EQUITY),
        "parent_equity": _find_item_by_codes(data, CODES_PARENT_EQUITY),
        "share_capital": _find_item_by_codes(data, CODES_SHARE_CAPITAL),
        "ppe": _find_item_by_codes(data, CODES_PP_AND_E),
    }


def _extract_cashflow_fields(data: list[dict]) -> dict:
    """Extract key fields from cash flow statement JSON.

    Args:
        data: Parsed cash flow statement JSON items.

    Returns:
        Dict with extracted values (may contain None).
    """
    return {
        "cfo": _find_item_by_codes(data, CODES_CFO),
        "da": _find_item_by_codes(data, CODES_DA_CASHFLOW),
        "capex": _find_item_by_codes(data, CODES_CAPEX),
        "fcf": _find_item_by_codes(data, CODES_FCF),
        "wc_change": _find_item_by_codes(data, CODES_WC_CHANGE),
    }
