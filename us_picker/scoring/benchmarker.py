"""Sectoral benchmarking module for BIST Stock Picker.

Calculates sector-level aggregated metrics (median, mean) to enable
relative valuation and UI benchmarking.
"""

import datetime
import logging
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from us_picker.db.schema import Company, AdjustedMetric, SectorBenchmark

logger = logging.getLogger(__name__)

class Benchmarker:
    """Calculates and manages sector-level performance benchmarks."""

    def save_benchmarks(self, session: Session, calculation_date: Optional[datetime.date] = None) -> int:
        """Calculate and persist sector benchmarks to the database."""
        calc_date = calculation_date or datetime.date.today()
        df = self.calculate_sector_medians(session)
        if df.empty:
            return 0

        # Delete existing for this date to allow re-runs
        session.query(SectorBenchmark).filter(SectorBenchmark.calculation_date == calc_date).delete()

        count = 0
        for sector, row in df.iterrows():
            # Count companies in this sector for metadata
            comp_count = int(session.query(Company).filter(
                Company.sector_custom == sector, 
                Company.is_active.is_(True)
            ).count())

            bm = SectorBenchmark(
                sector=str(sector),
                calculation_date=calc_date,
                roe_median=float(row["roe_median"]) if pd.notna(row["roe_median"]) else None,
                roa_median=float(row["roa_median"]) if pd.notna(row["roa_median"]) else None,
                net_margin_median=float(row["net_margin_median"]) if pd.notna(row["net_margin_median"]) else None,
                company_count=comp_count
            )
            session.add(bm)
            count += 1
        
        session.commit()
        logger.info("Saved %d sector benchmarks for %s", count, calc_date)
        return count

    def calculate_sector_medians(self, session: Session) -> pd.DataFrame:
        """Calculate median financial metrics for each custom sector.

        Uses the latest available adjusted metrics for all active companies.
        
        Returns:
            DataFrame with sector_custom as index and metric columns.
        """
        # Load active companies with their sectors
        companies = (
            session.query(Company.id, Company.ticker, Company.sector_custom)
            .filter(Company.is_active.is_(True))
            .all()
        )
        if not companies:
            return pd.DataFrame()

        comp_df = pd.DataFrame(companies, columns=["id", "ticker", "sector"])
        
        # Load latest metrics for these companies
        # We'll use a subquery to get the most recent period_end per company
        from sqlalchemy import func
        subq = (
            session.query(
                AdjustedMetric.company_id,
                func.max(AdjustedMetric.period_end).label("max_period")
            )
            .group_by(AdjustedMetric.company_id)
            .subquery()
        )
        
        metrics = (
            session.query(AdjustedMetric)
            .join(subq, (AdjustedMetric.company_id == subq.c.company_id) & 
                        (AdjustedMetric.period_end == subq.c.max_period))
            .all()
        )
        
        if not metrics:
            return pd.DataFrame()

        metric_data = []
        for m in metrics:
            metric_data.append({
                "company_id": m.company_id,
                "roe": m.roe_real if m.roe_real is not None else m.roe_adjusted,
                "roa": m.roa_real if m.roa_real is not None else m.roa_adjusted,
                "net_margin": m.adjusted_net_income / m.owner_earnings if m.owner_earnings and m.owner_earnings != 0 else None,
                # Note: price-based ratios (P/E, P/B) need current price, 
                # which isn't in adjusted_metrics. We'll stick to operational for now.
            })
        
        met_df = pd.DataFrame(metric_data)
        
        # Merge and group by sector
        merged = comp_df.merge(met_df, left_on="id", right_on="company_id")
        
        # Calculate medians (more robust than mean for sectors with outliers)
        sector_benchmarks = merged.groupby("sector")[["roe", "roa", "net_margin"]].median()
        sector_benchmarks.columns = ["roe_median", "roa_median", "net_margin_median"]
        
        return sector_benchmarks
