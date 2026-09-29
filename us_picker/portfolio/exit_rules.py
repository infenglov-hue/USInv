"""Exit rules checker for portfolio management.

Evaluates open positions against three exit conditions:
1. Stop-loss: Price falls below stop-loss level (default 82% of entry).
2. Target hit: Price reaches target price (DCF or composite-implied).
3. Thesis breaker: Significant negative news or insider selling.

Since we don't have full news sentiment analysis, 'thesis breaker' is
proxied by:
- Significant net insider selling since entry: more than
  max(5M TRY, 0.5% of market cap). The floor keeps small caps from
  triggering on routine trades; the percentage scales the bar up for
  large caps where 5M TRY is noise.
"""

import logging
from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    DailyPrice,
    InsiderTransaction,
    PortfolioSelection,
)

logger = logging.getLogger("us_picker.portfolio.exit_rules")

_STOP_LOSS_PCT = 0.82  # Hard stop at 18% loss if not specified
_THESIS_INSIDER_SELL_FLOOR = 5_000_000.0  # TRY floor — small caps
_THESIS_INSIDER_SELL_MCAP_PCT = 0.005     # 0.5% of market cap — large caps

# B1 trailing stop (2026-07-05): distance = 2×ATR/price clamped to the
# configured band, anchored at the highest close since entry. The stop only
# ratchets UP — a falling price never widens it back down.
# A/B VERDICT: production runs ENABLED with a 20-35% clamp (best risk
# metrics, return within 4% of no-trailing). The tight 10-25% clamp
# collapsed the same backtest to +852% vs +2958% — these module fallbacks
# are only used when thresholds.yaml is unreadable.
_TRAIL_MIN_PCT = 0.20
_TRAIL_MAX_PCT = 0.35
_TRAIL_ATR_MULT = 2.0


def _load_trailing_config() -> dict:
    """Read selection.trailing_stop from thresholds.yaml (safe defaults)."""
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = ((yaml.safe_load(f) or {}).get("selection", {}) or {}).get(
                "trailing_stop", {}
            ) or {}
    except OSError:
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "min_pct": float(cfg.get("min_pct", _TRAIL_MIN_PCT)),
        "max_pct": float(cfg.get("max_pct", _TRAIL_MAX_PCT)),
    }


class ExitRuleChecker:
    """Evaluates exit conditions for open portfolio positions."""

    def __init__(self, session: Session):
        self.session = session
        self._risk_classifier = None  # lazy — only needed for market cap

    def _insider_threshold(self, company_id: int) -> float:
        """Net-selling threshold scaled by market cap.

        max(5M TRY, 0.5% of market cap); falls back to the flat floor
        when market cap cannot be computed.
        """
        if self._risk_classifier is None:
            from us_picker.classification.risk_classifier import RiskClassifier

            self._risk_classifier = RiskClassifier()
        mcap = self._risk_classifier.compute_market_cap(company_id, self.session)
        if mcap is None or mcap <= 0:
            return _THESIS_INSIDER_SELL_FLOOR
        return max(_THESIS_INSIDER_SELL_FLOOR, mcap * _THESIS_INSIDER_SELL_MCAP_PCT)

    def update_trailing_stops(self, *, force_enabled: bool | None = None) -> int:
        """Track high-water marks and (if enabled) ratchet trailing stops.

        ``highest_close`` bookkeeping ALWAYS runs for open positions. The
        stop raise itself is gated by ``selection.trailing_stop.enabled``
        (thresholds.yaml, default OFF — see the 2026-07-05 A/B verdict in
        the config comment). When raising: stop becomes ``max(current_stop,
        highest_close × (1 − trail_pct))`` with ``trail_pct =
        clamp(2×ATR/price, min_pct, max_pct)``; falls back to max_pct when
        ATR is unavailable.

        Mutates the session (no commit); returns the number of stops raised.
        Call this BEFORE :meth:`check_exits` so today's evaluation uses the
        ratcheted level. ``force_enabled`` overrides the config (tests).
        """
        from us_picker.scoring.factors.technical import TechnicalScorer

        trail_cfg = _load_trailing_config()
        enabled = trail_cfg["enabled"] if force_enabled is None else force_enabled
        trail_min = trail_cfg["min_pct"]
        trail_max = trail_cfg["max_pct"]

        tech = TechnicalScorer()
        open_positions = (
            self.session.query(PortfolioSelection)
            .filter(PortfolioSelection.exit_date.is_(None))
            .all()
        )

        raised = 0
        for selection in open_positions:
            latest = (
                self.session.query(DailyPrice)
                .filter(
                    DailyPrice.company_id == selection.company_id,
                    DailyPrice.close.isnot(None),
                )
                .order_by(DailyPrice.date.desc())
                .first()
            )
            if latest is None or not latest.close or latest.close <= 0:
                continue

            close = float(latest.close)
            previous_high = selection.highest_close or selection.entry_price or close
            highest = max(float(previous_high), close)
            selection.highest_close = highest

            if not enabled:
                continue  # bookkeeping only — stop raise is config-gated

            try:
                atr = tech.calculate_atr(
                    selection.company_id, self.session, scoring_date=latest.date
                )
            except Exception:
                atr = None
            if atr and close > 0:
                trail_pct = min(
                    trail_max,
                    max(trail_min, (_TRAIL_ATR_MULT * float(atr)) / close),
                )
            else:
                trail_pct = trail_max

            candidate_stop = highest * (1.0 - trail_pct)
            current_stop = selection.stop_loss_price or 0.0
            if candidate_stop > current_stop:
                selection.stop_loss_price = round(candidate_stop, 4)
                raised += 1
                logger.info(
                    "Trailing stop raised for company %s: %.2f -> %.2f "
                    "(high %.2f, trail %.0f%%)",
                    selection.company_id,
                    current_stop,
                    candidate_stop,
                    highest,
                    trail_pct * 100,
                )

        return raised

    def check_exits(self) -> list[dict]:
        """Scan all open positions for exit signals.

        Returns:
            List of exit signals (dicts) with details needed for reporting.
            Does NOT execute the exit (update DB) — that is a manual decision
            or separate execution step.
        """
        open_positions = (
            self.session.query(PortfolioSelection, Company)
            .join(Company, Company.id == PortfolioSelection.company_id)
            .filter(PortfolioSelection.exit_date.is_(None))
            .all()
        )

        signals = []
        for selection, company in open_positions:
            signal = self._evaluate_position(selection, company)
            if signal:
                signals.append(signal)

        return signals

    def _evaluate_position(
        self, selection: PortfolioSelection, company: Company
    ) -> Optional[dict]:
        """Check a single position for any exit trigger."""
        # Get latest price
        latest_price_row = (
            self.session.query(DailyPrice)
            .filter(DailyPrice.company_id == company.id)
            .order_by(DailyPrice.date.desc())
            .first()
        )
        if not latest_price_row or not latest_price_row.close:
            return None

        current_price = latest_price_row.close
        price_date = latest_price_row.date
        entry_price = selection.entry_price or current_price  # Fallback to avoid div/0

        # Calculate return
        ret_pct = (current_price - entry_price) / entry_price * 100.0

        # 1. Stop-Loss Check
        # Use stored stop_loss or default 18% trailing/fixed
        stop_price = selection.stop_loss_price or (entry_price * _STOP_LOSS_PCT)
        if current_price <= stop_price:
            return {
                "company_id": company.id,
                "ticker": company.ticker,
                "portfolio": selection.portfolio,
                "entry_date": selection.selection_date,
                "entry_price": entry_price,
                "current_price": current_price,
                "price_date": price_date,
                "return_pct": ret_pct,
                "reason": "STOP_LOSS",
                "details": f"Price {current_price:.2f} <= Stop {stop_price:.2f}",
            }

        # 2. Target Hit Check
        if selection.target_price and current_price >= selection.target_price:
            return {
                "company_id": company.id,
                "ticker": company.ticker,
                "portfolio": selection.portfolio,
                "entry_date": selection.selection_date,
                "entry_price": entry_price,
                "current_price": current_price,
                "price_date": price_date,
                "return_pct": ret_pct,
                "reason": "TARGET",
                "details": f"Price {current_price:.2f} >= Target {selection.target_price:.2f}",
            }

        # 3. Thesis Breaker (Insider Selling)
        # Check net insider selling since entry date
        insider_net = self._calculate_net_insider_flow(
            company.id, selection.selection_date
        )
        if insider_net <= -self._insider_threshold(company.id):
            return {
                "company_id": company.id,
                "ticker": company.ticker,
                "portfolio": selection.portfolio,
                "entry_date": selection.selection_date,
                "entry_price": entry_price,
                "current_price": current_price,
                "price_date": price_date,
                "return_pct": ret_pct,
                "reason": "THESIS_BREAKER",
                "details": (
                    f"Significant insider selling: {insider_net:,.0f} TRY "
                    f"since entry."
                ),
            }

        return None

    def _calculate_net_insider_flow(
        self, company_id: int, since_date: date
    ) -> float:
        """Calculate net insider transaction value (BUY - SELL) since a date.

        Returns:
            Net value in TRY. Negative means net selling.
        """
        txs = (
            self.session.query(InsiderTransaction)
            .filter(
                InsiderTransaction.company_id == company_id,
                InsiderTransaction.disclosure_date >= since_date,
            )
            .all()
        )

        net_flow = 0.0
        for tx in txs:
            if not tx.total_value_try:
                continue

            val = tx.total_value_try
            if tx.transaction_type == "BUY":
                net_flow += val
            elif tx.transaction_type == "SELL":
                net_flow -= val

        return net_flow
