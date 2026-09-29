
import json
import logging
from datetime import date
from collections import defaultdict
from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session
from sqlalchemy import func

from us_picker.cleaning.financial_periods import (
    AnalyticalStatement,
    analytical_statement_from_map,
    latest_statement_map,
)
from us_picker.db.schema import (
    AdjustedMetric,
    Company,
    DailyPrice,
    FinancialStatement,
)

logger = logging.getLogger(__name__)

# Filing-lag estimates for rows without a real publication_date. KAP's
# disclosure-history API is internal-only (see data/sources/kap.py), so real
# filing dates are unavailable for backfill; instead we assume every company
# files exactly at its SPK Seri II-14.1 deadline (consolidated issuer) plus
# a small safety buffer:
#   Q1 / Q3 interim (unaudited):     40 days -> 46 with buffer
#   H1 interim (limited review):     50 days -> 56 with buffer
#   Annual (audited) + non-standard: 70 days -> 76 with buffer (equals the
#                                    old flat 76-day heuristic, so annual
#                                    behavior is unchanged)
# Deadline-timing is conservative for on-time filers; companies filing past
# the SPK deadline can still leak — a known, documented limitation.
_ESTIMATED_LAG_QUARTER_DAYS = 46
_ESTIMATED_LAG_H1_DAYS = 56
_LEGACY_PUBLICATION_LAG_DAYS = 76


def estimated_metric_visibility_date(period_end: date) -> date:
    """Return the conservative PIT date for a metric without a filing date."""
    from datetime import timedelta

    if period_end.month in (3, 9):
        lag_days = _ESTIMATED_LAG_QUARTER_DAYS
    elif period_end.month == 6:
        lag_days = _ESTIMATED_LAG_H1_DAYS
    else:
        lag_days = _LEGACY_PUBLICATION_LAG_DAYS
    return period_end + timedelta(days=lag_days)


def _estimated_filing_lag_filter(period_end_column, scoring_date: date):
    """SQLAlchemy filter: period visible once its SPK filing deadline passed.

    Month of ``period_end`` decides the deadline: 3/9 → quarter lag,
    6 → half-year lag, everything else (12 + odd fiscal year ends) → the
    conservative annual lag.
    """
    from datetime import timedelta
    from sqlalchemy import and_, extract, or_

    month = extract("month", period_end_column)
    return or_(
        and_(
            month.in_([3, 9]),
            period_end_column
            <= scoring_date - timedelta(days=_ESTIMATED_LAG_QUARTER_DAYS),
        ),
        and_(
            month == 6,
            period_end_column
            <= scoring_date - timedelta(days=_ESTIMATED_LAG_H1_DAYS),
        ),
        and_(
            month.notin_([3, 6, 9]),
            period_end_column
            <= scoring_date - timedelta(days=_LEGACY_PUBLICATION_LAG_DAYS),
        ),
    )


def _adjusted_metric_pit_filter(scoring_date: date):
    """SQLAlchemy filter: AdjustedMetric rows knowable on or before scoring_date.

    Used by ScoringContext + every per-scorer fallback path (Buffett /
    Graham / DCF) + UniverseBuilder. Centralized here so the lag heuristic
    lives in ONE place and can be retired once real publication dates exist.

    Mixed mode (audit CRITICAL #1, 2026-05-07):
      * Rows with ``publication_date IS NOT NULL`` → strict
        ``publication_date <= scoring_date``.
      * Rows with ``publication_date IS NULL`` → SPK-deadline estimate per
        period type (2026-07-02; previously a flat 76-day lag that kept
        Q1/Q3 data invisible for ~5 extra weeks).
    """
    from sqlalchemy import and_, or_

    return or_(
        and_(
            AdjustedMetric.publication_date.isnot(None),
            AdjustedMetric.publication_date <= scoring_date,
        ),
        and_(
            AdjustedMetric.publication_date.is_(None),
            _estimated_filing_lag_filter(AdjustedMetric.period_end, scoring_date),
        ),
    )


def analytical_metric_series(
    metrics: List[AdjustedMetric],
) -> List[AdjustedMetric]:
    """Annual history plus the latest visible interim TTM observation.

    Four overlapping TTM rows per year must not be interpreted as four years
    by Buffett/DCF/Lynch trend formulas. Annual history preserves their
    calibration; the newest interim row updates the current state.
    """

    if not metrics:
        return []
    ordered = sorted(metrics, key=lambda metric: metric.period_end)
    annual = []
    for metric in ordered:
        source_period_type = getattr(metric, "source_period_type", None)
        # Legacy rows and lightweight test/research fixtures may not carry the
        # additive audit column. Non-string mock attributes are also legacy.
        if not isinstance(source_period_type, str):
            source_period_type = None
        if source_period_type == "ANNUAL" or (
            source_period_type is None and metric.period_end.month == 12
        ):
            annual.append(metric)
    latest = ordered[-1]
    if latest.period_end.month != 12 and latest not in annual:
        annual.append(latest)
    return annual


class ScoringContext:
    """Holds pre-fetched data for a batch of companies to avoid N+1 queries.
    
    Loads AdjustedMetrics, FinancialStatements, and latest Prices in bulk.
    """

    def __init__(self, session: Session, scoring_date: Optional[date] = None):
        self.session = session
        self.scoring_date = scoring_date
        
        # Data caches: {company_id: [objects]}
        self._metrics: Dict[int, List[AdjustedMetric]] = defaultdict(list)
        self._statements: Dict[int, Dict[str, List[FinancialStatement]]] = defaultdict(lambda: defaultdict(list))
        self._analytical_statements: Dict[
            tuple[int, str, date], Optional[AnalyticalStatement]
        ] = {}
        self._prices: Dict[int, float] = {}
        self._company_types: Dict[int, str] = {}
        
        # Track loaded IDs to avoid reload
        self._loaded_ids = set()

    def load_data(self, company_ids: List[int]) -> None:
        """Bulk load data for the given company IDs."""
        ids_to_load = [cid for cid in company_ids if cid not in self._loaded_ids]
        if not ids_to_load:
            return

        logger.info(f"Pre-fetching data for {len(ids_to_load)} companies...")

        # 1. Company Types (for filtering)
        companies = (
            self.session.query(Company.id, Company.company_type)
            .filter(Company.id.in_(ids_to_load))
            .all()
        )
        for cid, ctype in companies:
            self._company_types[cid] = (ctype or "").upper()

        # 2. Adjusted Metrics — point-in-time guard (audit CRITICAL #1,
        # 2026-05-07): prefer the row's own publication_date over the
        # legacy 76-day heuristic. Mixed mode lets us migrate gradually:
        # new rows have a real filing date, old rows fall back to the
        # heuristic so we don't suddenly drop years of history.
        from sqlalchemy import or_
        cutoff_date = self.scoring_date or date.today()
        query = (
            self.session.query(AdjustedMetric)
            .filter(
                AdjustedMetric.company_id.in_(ids_to_load),
                _adjusted_metric_pit_filter(cutoff_date),
            )
        )

        metrics = query.order_by(AdjustedMetric.period_end).all()
        for m in metrics:
            self._metrics[m.company_id].append(m)

        # 3. Financial Statements. Load every PIT-visible period because the
        # latest interim analytical view needs current YTD + prior annual +
        # prior-year same YTD. Accessors expose annual history + latest TTM so
        # factor trend windows are not quarter-density biased.
        cutoff = self.scoring_date or date.today()
        stmt_query = (
            self.session.query(FinancialStatement)
            .filter(
                FinancialStatement.company_id.in_(ids_to_load),
            )
        )
        from sqlalchemy import and_, or_
        stmt_query = stmt_query.filter(
            or_(
                and_(
                    FinancialStatement.publication_date.isnot(None),
                    FinancialStatement.publication_date <= cutoff,
                ),
                and_(
                    FinancialStatement.publication_date.is_(None),
                    _estimated_filing_lag_filter(
                        FinancialStatement.period_end,
                        cutoff,
                    ),
                ),
            )
        )
            
        statements = stmt_query.order_by(
            FinancialStatement.period_end,
            FinancialStatement.statement_type,
            FinancialStatement.version,
        ).all()
        for s in statements:
            # Skip shell records with all-null values (e.g. future period 2025/12
            # fetched before the company has filed its annual report).
            if s.data_json:
                try:
                    import json
                    items = json.loads(s.data_json)
                    if isinstance(items, list) and not any(
                        item.get("value") is not None for item in items
                    ):
                        continue  # All values null — skip this record
                except (json.JSONDecodeError, TypeError):
                    pass
            self._statements[s.company_id][s.statement_type].append(s)

        # 4. Latest Prices
        # We need the latest price ON or BEFORE scoring_date.
        # Subquery strategy for bulk latest price is efficient.
        # SELECT company_id, close FROM daily_prices WHERE (company_id, date) IN ...
        # Or simpler: Window function? SQLite supports window functions.
        
        # Max date per company <= scoring_date
        max_date_sq = (
            self.session.query(
                DailyPrice.company_id, 
                func.max(DailyPrice.date).label("max_date")
            )
            .filter(DailyPrice.company_id.in_(ids_to_load))
            .filter(DailyPrice.close.isnot(None))
        )
        if self.scoring_date:
            max_date_sq = max_date_sq.filter(DailyPrice.date <= self.scoring_date)
            
        max_date_sq = max_date_sq.group_by(DailyPrice.company_id).subquery()
        
        prices = (
            self.session.query(DailyPrice.company_id, DailyPrice.close, DailyPrice.date)
            .join(max_date_sq, 
                  (DailyPrice.company_id == max_date_sq.c.company_id) & 
                  (DailyPrice.date == max_date_sq.c.max_date))
            .all()
        )

        # US port: valuation prices are in base units (split-normalized) so
        # they match the share counts in the statements.
        from us_picker.utils.splits import cumulative_split_factor

        for cid, close_price, price_date in prices:
            self._prices[cid] = (
                close_price * cumulative_split_factor(self.session, cid, price_date)
                if close_price is not None
                else None
            )

        self._loaded_ids.update(ids_to_load)

    # --- Accessors ---

    def get_company_type(self, company_id: int) -> str:
        return self._company_types.get(company_id, "")

    def get_metrics(self, company_id: int) -> List[AdjustedMetric]:
        """Get annual history plus latest visible TTM, ordered by date."""
        return analytical_metric_series(self._metrics.get(company_id, []))

    def get_all_metrics(self, company_id: int) -> List[AdjustedMetric]:
        """Get every visible annual/TTM row for comparable-period logic."""
        return list(self._metrics.get(company_id, []))

    def get_latest_and_yoy_metrics(
        self, company_id: int,
    ) -> tuple[Optional[AdjustedMetric], Optional[AdjustedMetric]]:
        """Return latest metric and exactly the same reporting point one year ago."""

        metrics = self.get_all_metrics(company_id)
        if not metrics:
            return None, None
        current = metrics[-1]
        comparable = next(
            (
                metric
                for metric in reversed(metrics[:-1])
                if metric.period_end.month == current.period_end.month
                and metric.period_end.day == current.period_end.day
                and metric.period_end.year == current.period_end.year - 1
            ),
            None,
        )
        return current, comparable

    def get_statement(
        self,
        company_id: int,
        statement_type: str,
        period_end: date,
    ) -> Optional[AnalyticalStatement]:
        """Get one exact annual/TTM analytical statement from the preload cache."""

        key = (company_id, statement_type, period_end)
        if key not in self._analytical_statements:
            rows: list[FinancialStatement] = []
            for values in self._statements[company_id].values():
                rows.extend(values)
            self._analytical_statements[key] = analytical_statement_from_map(
                latest_statement_map(rows),
                company_id,
                period_end,
                statement_type,
            )
        return self._analytical_statements[key]

    def get_statements(
        self, company_id: int, statement_type: str,
    ) -> List[AnalyticalStatement]:
        """Get annual history + latest interim TTM aligned to get_metrics()."""

        statements: list[AnalyticalStatement] = []
        for metric in self.get_metrics(company_id):
            statement = self.get_statement(
                company_id,
                statement_type,
                metric.period_end,
            )
            if statement is not None:
                statements.append(statement)
        return statements

    def get_latest_price(self, company_id: int) -> Optional[float]:
        return self._prices.get(company_id)
