"""Price momentum scoring factor for BIST Stock Picker.

Calculates 3-month, 6-month, and 12-month price momentum with the
academic 1-month skip (short-term reversal effect). The most recent
month is excluded from all windows by default.

"3-month momentum" = return from month-4 to month-1
"6-month momentum" = return from month-7 to month-1
"12-month momentum" = return from month-13 to month-1

Combined momentum = weighted: 12m (40%), 6m (30%), 3m (30%)

The skip length comes from ``thresholds.yaml`` (``momentum.skip_days``,
default 30). Falling-knife protection is NOT this factor's job — the
selector already drops crashed names via the raw 200MA + technical
percentile filter, so the skip can stay purely about reversal noise.

The raw returns are computed per-company; percentile normalization
across the full universe should be done at the composer level.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from us_picker.db.schema import Company, DailyPrice

logger = logging.getLogger("us_picker.scoring.factors.momentum")

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "thresholds.yaml"
)

# Approximate trading days per month
_TRADING_DAYS_PER_MONTH = 21

# Default 1-month skip when thresholds.yaml has no momentum section
_DEFAULT_SKIP_DAYS = 30

# Weights for combined momentum
_WEIGHT_3M = 0.30
_WEIGHT_6M = 0.30
_WEIGHT_12M = 0.40


class MomentumScorer:
    """Calculates price momentum with 1-month skip for reversal effect.

    Returns raw returns per company. Percentile normalization across
    the universe should be applied at the scoring composer level.

    Args:
        config_path: Path to thresholds.yaml. Defaults to config/thresholds.yaml.
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        path = config_path or _DEFAULT_CONFIG_PATH
        cfg: dict = {}
        try:
            with open(path, encoding="utf-8") as f:
                cfg = (yaml.safe_load(f) or {}).get("momentum", {})
        except OSError:
            logger.warning("thresholds.yaml not readable at %s; using momentum defaults", path)
        self._skip_days = int(cfg.get("skip_days", _DEFAULT_SKIP_DAYS))

    def score(
        self,
        company_id: int,
        session: Session,
        scoring_date: Optional[date] = None,
    ) -> Optional[dict]:
        """Calculate momentum scores for a single company.

        Args:
            company_id: Database ID of the company.
            session: SQLAlchemy session.
            scoring_date: Date to score from (the "current" date).

        Returns:
            Dict with return_3m, return_6m, return_12m, momentum_combined,
            or None if insufficient price data.
        """
        company = session.get(Company, company_id)
        if company is None:
            return None

        query = session.query(DailyPrice.date).filter(
            DailyPrice.company_id == company_id,
            (DailyPrice.adjusted_close.isnot(None)) | (DailyPrice.close.isnot(None)),
        )
        if scoring_date:
            query = query.filter(DailyPrice.date <= scoring_date)

        latest_row = query.order_by(DailyPrice.date.desc()).first()
        if latest_row is None:
            return None

        latest_date = latest_row[0]

        # Academic 12-1 momentum: skip the most recent month so short-term
        # reversal noise does not contaminate the trend signal. Falling-knife
        # protection lives in the selector (raw 200MA filter), not here.
        # skip_days=0 in thresholds.yaml restores the no-skip behavior.
        skip_date = latest_date - timedelta(days=self._skip_days)

        # Get the price at the end of momentum window. Strictly backward:
        # a forward fallback would silently shrink the skip window on
        # illiquid/suspended names (2026-05-07 audit, HIGH #7).
        end_price = self._get_price_near(company_id, skip_date, session, direction="backward")
        if end_price is None:
            return None

        # Calculate returns for each lookback period
        return_3m = self._calc_return(
            company_id, skip_date, months=3, end_price=end_price, session=session,
        )
        return_6m = self._calc_return(
            company_id, skip_date, months=6, end_price=end_price, session=session,
        )
        return_12m = self._calc_return(
            company_id, skip_date, months=12, end_price=end_price, session=session,
        )

        # Need at least 3-month momentum to produce a result
        if return_3m is None:
            return None

        # Weighted combined (use available data)
        parts = []
        weights = []
        if return_12m is not None:
            parts.append(return_12m * _WEIGHT_12M)
            weights.append(_WEIGHT_12M)
        if return_6m is not None:
            parts.append(return_6m * _WEIGHT_6M)
            weights.append(_WEIGHT_6M)
        if return_3m is not None:
            parts.append(return_3m * _WEIGHT_3M)
            weights.append(_WEIGHT_3M)

        total_weight = sum(weights)
        combined = sum(parts) / total_weight if total_weight > 0 else None

        return {
            "return_3m": return_3m,
            "return_6m": return_6m,
            "return_12m": return_12m,
            "momentum_combined": combined,
        }

    def score_all(
        self,
        session: Session,
        scoring_date: Optional[date] = None,
        company_ids: Optional[list[int]] = None,
    ) -> dict[int, dict]:
        """Score all active companies.

        Returns raw momentum_combined per company; universe-wide percentile
        normalization is applied downstream by ScoreComposer, so this method
        intentionally does not compute its own percentile ranks.

        ``company_ids`` restricts scoring to an explicit universe. Point-in-time
        backtests pass their as-of membership so names that were still listed on
        ``scoring_date`` but have since delisted get a momentum score too — the
        default ``is_active`` query silently drops them, which left resurrected
        survivorship names with a value-only composite. ``None`` keeps the live
        behavior (all currently active companies).
        """
        if company_ids is not None:
            ids = list(company_ids)
        else:
            ids = [
                cid
                for (cid,) in session.query(Company.id)
                .filter(Company.is_active.is_(True))
                .all()
            ]

        raw_scores = {}
        for cid in ids:
            result = self.score(cid, session, scoring_date)
            if result is not None:
                raw_scores[cid] = result

        return raw_scores

    def _calc_return(
        self,
        company_id: int,
        end_date: date,
        months: int,
        end_price: float,
        session: Session,
    ) -> Optional[float]:
        """Calculate price return over a period ending at end_date.

        Args:
            company_id: Company DB ID.
            end_date: End date of the momentum window (already skip-adjusted).
            months: Number of months to look back.
            end_price: Price at end_date.
            session: SQLAlchemy session.

        Returns:
            Return as decimal (e.g., 0.15 for 15%), or None if no start price.
        """
        start_date = end_date - timedelta(days=months * 30)
        start_price = self._get_price_near(company_id, start_date, session)

        if start_price is None or start_price <= 0:
            return None

        return (end_price / start_price) - 1.0

    def _get_price_near(
        self,
        company_id: int,
        target_date: date,
        session: Session,
        tolerance_days: int = 10,
        direction: str = "both",
    ) -> Optional[float]:
        """Get the closing price nearest to target_date.

        Searches within tolerance_days before or after the target date
        depending on direction.

        Args:
            company_id: Company DB ID.
            target_date: Target date.
            session: SQLAlchemy session.
            tolerance_days: Max days to search.
            direction: Search direction: 'backward', 'forward', or 'both'.

        Returns:
            Closing price, or None if not found.
        """
        # Use adjusted_close (split-adjusted) with fallback to raw close.
        # Raw close shows phantom drops after stock splits / capital increases.

        # Try before target date first (most common for market days)
        if direction in ("both", "backward"):
            before = (
                session.query(DailyPrice.adjusted_close, DailyPrice.close)
                .filter(
                    DailyPrice.company_id == company_id,
                    DailyPrice.date <= target_date,
                    DailyPrice.date >= target_date - timedelta(days=tolerance_days),
                    (DailyPrice.adjusted_close.isnot(None)) | (DailyPrice.close.isnot(None)),
                )
                .order_by(DailyPrice.date.desc())
                .first()
            )
            if before:
                return before[0] if before[0] is not None else before[1]

        # Try after target date
        if direction in ("both", "forward"):
            after = (
                session.query(DailyPrice.adjusted_close, DailyPrice.close)
                .filter(
                    DailyPrice.company_id == company_id,
                    DailyPrice.date >= target_date,
                    DailyPrice.date <= target_date + timedelta(days=tolerance_days),
                    (DailyPrice.adjusted_close.isnot(None)) | (DailyPrice.close.isnot(None)),
                )
                .order_by(DailyPrice.date.asc())
                .first()
            )
            if after:
                return after[0] if after[0] is not None else after[1]

        return None
