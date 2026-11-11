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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    DailyPrice,
    InsiderTransaction,
    PortfolioSelection,
    ScoringResult,
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


def _load_exit_selection_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("selection", {}) or {}
    except OSError:
        return {}


def _load_trailing_config() -> dict:
    """Read selection.trailing_stop from thresholds.yaml (safe defaults)."""
    cfg = _load_exit_selection_config().get("trailing_stop", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "min_pct": float(cfg.get("min_pct", _TRAIL_MIN_PCT)),
        "max_pct": float(cfg.get("max_pct", _TRAIL_MAX_PCT)),
    }


@dataclass(frozen=True)
class ExitBreakers:
    time_enabled: bool
    min_holding_days: int
    max_lag_pct: float
    fundamental_enabled: bool
    min_fscore: int

    @classmethod
    def load(cls, overrides: dict | None = None) -> "ExitBreakers":
        selection = _load_exit_selection_config()
        overrides = overrides or {}
        for key in ("time_breaker", "fundamental_breaker"):
            selection[key] = {**(selection.get(key) or {}), **(overrides.get(key) or {})}
        return cls.from_selection(selection)

    @classmethod
    def from_selection(cls, selection: dict) -> "ExitBreakers":
        time = selection.get("time_breaker") or {}
        fundamental = selection.get("fundamental_breaker") or {}
        return cls(
            time_enabled=time.get("enabled") is True,
            min_holding_days=int(time.get("min_holding_days", 60)),
            max_lag_pct=float(time.get("max_lag_vs_index_pct", 10.0)),
            fundamental_enabled=fundamental.get("enabled") is True,
            min_fscore=int(fundamental.get("min_fscore", 4)),
        )

    def time_triggered(self, holding_days: int, stock_return_pct: float, spy_return_pct: float) -> bool:
        return (
            self.time_enabled
            and holding_days >= self.min_holding_days
            and spy_return_pct - stock_return_pct >= self.max_lag_pct
        )

    def fundamental_triggered(self, fscore: int | None) -> bool:
        return self.fundamental_enabled and fscore is not None and fscore < self.min_fscore


class ExitRuleChecker:
    """Evaluates exit conditions for open portfolio positions."""

    def __init__(self, session: Session, *, completed_session: date | None = None):
        self.session = session
        self._completed_session = completed_session
        self._risk_classifier = None  # lazy — only needed for market cap
        self._breakers = ExitBreakers.load()

    def _insider_threshold(self, company_id: int, as_of: date | None = None) -> float:
        """Net-selling threshold scaled by market cap.

        max(5M TRY, 0.5% of market cap); falls back to the flat floor
        when market cap cannot be computed.
        """
        if self._risk_classifier is None:
            from us_picker.classification.risk_classifier import RiskClassifier

            self._risk_classifier = RiskClassifier()
        mcap = self._risk_classifier.compute_market_cap(company_id, self.session, scoring_date=as_of)
        if mcap is None or mcap <= 0:
            return _THESIS_INSIDER_SELL_FLOOR
        return max(_THESIS_INSIDER_SELL_FLOOR, mcap * _THESIS_INSIDER_SELL_MCAP_PCT)

    def _position_bar(self, selection: PortfolioSelection) -> DailyPrice | None:
        """Find the traded close eligible for this position's current rules."""
        effective_date = max(
            selection.selection_date,
            selection.cycle_ref_date or selection.selection_date,
            selection.split_applied_through or selection.selection_date,
        )
        query = self.session.query(DailyPrice).filter(
            DailyPrice.company_id == selection.company_id,
            DailyPrice.date >= effective_date,
            DailyPrice.close > 0,
        )
        if self._completed_session is not None:
            query = query.filter(DailyPrice.date == self._completed_session)
        return query.order_by(DailyPrice.date.desc()).first()

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
            latest = self._position_bar(selection)
            if latest is None:
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
        latest_price_row = self._position_bar(selection)
        if latest_price_row is None:
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
            company.id, selection.selection_date, price_date
        )
        if insider_net < 0 and insider_net <= -self._insider_threshold(company.id, price_date):
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

        reason, details = self._evaluate_breakers(selection, company, latest_price_row)
        if reason is None:
            return None
        return {
            "company_id": company.id,
            "ticker": company.ticker,
            "portfolio": selection.portfolio,
            "entry_date": selection.selection_date,
            "entry_price": entry_price,
            "current_price": current_price,
            "price_date": price_date,
            "return_pct": ret_pct,
            "reason": reason,
            "details": details,
        }

    def _evaluate_breakers(
        self, selection: PortfolioSelection, company: Company, latest_price: DailyPrice
    ) -> tuple[str | None, str | None]:
        if self._breakers.fundamental_enabled:
            latest_score = (
                self.session.query(ScoringResult.piotroski_fscore_raw)
                .filter(
                    ScoringResult.company_id == company.id,
                    ScoringResult.scoring_date <= latest_price.date,
                )
                .order_by(ScoringResult.scoring_date.desc())
                .first()
            )
            if latest_score and self._breakers.fundamental_triggered(latest_score[0]):
                return "FUNDAMENTAL_BREAKER", f"Piotroski F-Score {latest_score[0]}/9"

        if not self._breakers.time_enabled or selection.selection_date is None:
            return None, None
        holding_days = (latest_price.date - selection.selection_date).days
        if holding_days < self._breakers.min_holding_days:
            return None, None

        from us_picker.utils.index_prices import get_spliced_price_by_ticker

        stock_entry = get_spliced_price_by_ticker(self.session, company.ticker, selection.selection_date)
        stock_close = get_spliced_price_by_ticker(self.session, company.ticker, latest_price.date)
        spy_entry = get_spliced_price_by_ticker(self.session, "SPY", selection.selection_date)
        spy_close = get_spliced_price_by_ticker(self.session, "SPY", latest_price.date)
        if min(stock_entry, stock_close, spy_entry, spy_close) <= 0:
            return None, None
        stock_return = (stock_close / stock_entry - 1.0) * 100
        spy_return = (spy_close / spy_entry - 1.0) * 100
        if self._breakers.time_triggered(holding_days, stock_return, spy_return):
            lag = spy_return - stock_return
            return "TIME_UNDERPERFORMANCE_BREAKER", f"Held {holding_days}d, trailing SPY by {lag:.1f} pp"
        return None, None

    def _calculate_net_insider_flow(
        self, company_id: int, since_date: date, through_date: date
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
                InsiderTransaction.disclosure_date <= through_date,
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
