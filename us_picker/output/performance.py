import logging
from datetime import date, timedelta
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from us_picker.db.schema import PortfolioSelection, DailyPrice, Company

logger = logging.getLogger(__name__)

class PerformanceTracker:
    """Tracks and calculates portfolio performance metrics."""

    def __init__(self, session: Session):
        self.session = session

    def calculate_portfolio_performance(self, portfolio_name: str) -> Dict[str, Any]:
        """Return canonical cohort/cycle-mark NAV metrics for a portfolio."""
        try:
            from us_picker.portfolio.performance_ledger import (
                calculate_live_performance,
            )
            return calculate_live_performance(self.session, portfolio_name)

        except Exception as e:
            logger.error(f"Error calculating portfolio performance: {e}")
            return {}

    def fetch_benchmark_performance(self) -> float:
        """Calculates XU100 YTD return (from Jan 1 of current year)."""
        try:
            xu100 = self.session.query(Company).filter(Company.ticker == "SPY").first()
            if not xu100:
                return 0.0

            today = date.today()
            start_of_year = date(today.year, 1, 1)

            latest = (
                self.session.query(DailyPrice)
                .filter(DailyPrice.company_id == xu100.id)
                .order_by(DailyPrice.date.desc())
                .first()
            )
            start = (
                self.session.query(DailyPrice)
                .filter(DailyPrice.company_id == xu100.id)
                .filter(DailyPrice.date >= start_of_year)
                .order_by(DailyPrice.date.asc())
                .first()
            )

            if latest and start and float(start.close) > 0:
                return ((float(latest.close) - float(start.close)) / float(start.close)) * 100
            
            return 0.0
        except Exception as e:
            logger.error(f"Error fetching benchmark performance: {e}")
            return 0.0

    def _get_current_prices(self, company_ids: List[int]) -> Dict[int, float]:
        """Fetches the latest available close price for given companies."""
        if not company_ids:
            return {}
        
        # Get latest price for each unique company_id
        unique_ids = list(set(company_ids))
        prices = {}
        for cid in unique_ids:
            price = (
                self.session.query(DailyPrice)
                .filter(DailyPrice.company_id == cid)
                .order_by(DailyPrice.date.desc())
                .first()
            )
            if price:
                prices[cid] = float(price.adjusted_close or price.close)
        return prices
