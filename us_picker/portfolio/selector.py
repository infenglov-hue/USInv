"""Portfolio selection module for the BIST Stock Picker.

Implements the PortfolioSelector that picks N stocks per portfolio
(ALPHA, BETA, DELTA) from the eligible universe. ``N`` is read from
``selection.target_count`` in thresholds.yaml (default 5).

  Constraints
  -----------
  * Max ``selection.max_per_sector`` stocks from the same custom sub-sector
  * Max ``selection.max_banks_per_portfolio`` banks per portfolio

  Turnover penalty
  ----------------
  An incumbent holding is retained when:
    (a) It appears in the top-10 candidates by composite score, AND
    (b) The best available new (non-incumbent) candidate does NOT score
        more than 15% higher than the incumbent.

  Target price
  ------------
  Derived from DCF margin-of-safety (dcf_margin_of_safety_pct) when
  available; otherwise falls back to a score-implied upside estimate.

  Stop loss
  ---------
  entry_price * 0.82 (fixed 18% stop).

Results are written to the portfolio_selections table via
select_and_store().
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from us_picker.db.schema import Company, DailyPrice, PortfolioSelection, ScoringResult
from us_picker.portfolio.cash_signal import (
    CashSignalCalculator,
    CashSignalResult,
)
from us_picker.portfolio.universes import UniverseBuilder
from us_picker.utils.index_prices import get_spliced_price_by_ticker

# Phase 5: human-friendly labels for the top-factor reason chips. Kept
# alongside the selector (not in the factor modules) because this mapping is a
# UI concern rather than a scoring concern. Only factors that end up on a 0-100
# scale after normalization are eligible — raw DCF MoS and raw PEG score are
# excluded because their scale is not comparable to the normalized factors.
_REASON_FACTOR_LABELS: dict[str, str] = {
    "buffett_score": "Buffett Quality",
    "graham_score": "Graham Value",
    "piotroski_fscore": "Piotroski",
    "magic_formula_rank": "Magic Formula",
    "momentum_score": "Momentum",
    "technical_score": "Technical",
    "dividend_score": "Dividend Yield",
    "banking_composite": "Banking Model",
    "holding_composite": "Holding Model",
    "reit_composite": "REIT Model",
}
# Cap the number of chips so the APK detail card stays scannable.
_REASON_TOP_N: int = 3

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
)

# ── Constants ─────────────────────────────────────────────────────────────────
# BETA/DELTA were removed 2026-05-07 (no YAML weights, never written).
_PORTFOLIO_SCORE_COL: dict[str, str] = {
    "ALPHA": "composite_alpha",
}
_PICKS_PER_PORTFOLIO: int = 5
# Phase 3 tightening (2026-04-18): 3 -> 2 so a single sector cannot dominate.
_MAX_PER_SECTOR: int = 2
_MAX_BANKS: int = 2
_TOP_N_FOR_TURNOVER: int = 10       # Incumbents protected if still in top 10
_TURNOVER_THRESHOLD: float = 1.15   # New candidate must be 15% better to replace
_STOP_LOSS_FACTOR: float = 0.82     # stop_loss = entry_price * 0.82 (fallback)
# Phase 3 tightening: 0.85 -> 0.70 for genuinely independent bets.
_MAX_CORRELATION: float = 0.70       # max pairwise correlation between picks
_CORRELATION_LOOKBACK: int = 120     # days of return history for correlation
_ATR_PERIOD: int = 20                # days for ATR calculation
_ATR_MULTIPLIER: float = 2.0         # stop = entry - (ATR × multiplier)
_MIN_STOP_PCT: float = 0.10          # minimum stop distance (10%)
_MAX_STOP_PCT: float = 0.25          # maximum stop distance (25%)

# Fallback upside range for score-implied target price
_MIN_UPSIDE: float = 0.10           # 10% minimum implied upside
_SCORE_UPSIDE_DIVISOR: float = 400  # 100-score stock -> 25% upside (100/400 = 0.25)
_DEFAULT_MAX_TARGET_MULTIPLE: float = 2.5  # cap: target <= entry * 2.5 (150% upside)
_TARGET_SOURCE_DCF: str = "DCF_INTRINSIC"
_TARGET_SOURCE_SCORE: str = "SCORE_IMPLIED"
_TARGET_SOURCE_DCF_CAPPED: str = "DCF_CAPPED"
_TARGET_SOURCE_SCORE_CAPPED: str = "SCORE_CAPPED"
# #11 (2026-07-07): volatility-scaled expected-move target for names without a
# DCF margin-of-safety — more defensible than a flat score×30% upside. Opt-in
# via selection.target_expected_move.enabled (default off = SCORE_IMPLIED).
_TARGET_SOURCE_ATR: str = "ATR_EXPECTED_MOVE"
_TARGET_SOURCE_ATR_CAPPED: str = "ATR_CAPPED"
_DEFAULT_TARGET_ATR_MULT: float = 3.0
_STRATEGY_CLASSIC = "classic"
_STRATEGY_INDEX_AWARE = "index_aware"
_DEFAULT_STRATEGY_VARIANT = _STRATEGY_INDEX_AWARE
_VALID_STRATEGY_VARIANTS = {_STRATEGY_CLASSIC, _STRATEGY_INDEX_AWARE}
_RELATIVE_STRENGTH_LOOKBACK_DAYS: int = 63
_INDEX_AWARE_MODEL_WEIGHT: float = 0.75
_INDEX_AWARE_RELATIVE_WEIGHT: float = 0.35
_INDEX_AWARE_TECHNICAL_WEIGHT: float = 0.05
_INDEX_AWARE_MIN_BIST100: int = 2
_INDEX_AWARE_MAX_NON_BIST100: int = 2
_INDEX_AWARE_MIN_DATA_COMPLETENESS: float = 60.0
_INDEX_AWARE_MIN_AVG_VOLUME_TRY: float = 10_000_000.0
_INDEX_AWARE_MIN_FREE_FLOAT: float = 25.0
_INDEX_AWARE_INCUMBENT_WINDOW: int = 18
_INDEX_AWARE_INCUMBENT_MIN_SCORE: float = 88.0
_INDEX_AWARE_FINANCIAL_SLEEVE_MIN_SCORE: float = 80.0
_INDEX_AWARE_FINANCIAL_SLEEVE_MIN_RELATIVE: float = 50.0


def _compute_reason_top_factors(
    factor_scores: dict[str, Optional[float]],
    top_n: int = _REASON_TOP_N,
) -> list[dict]:
    """Return the top-N contributing factor chips for a pick.

    Ranks the factors present in ``factor_scores`` by their normalized 0-100
    value and returns the top ``top_n`` as a list of
    ``{factor, label, value}`` dicts. Factors with ``None`` values are skipped
    so we don't surface phantom "0" chips when a scorer was unavailable.

    Stable tie-break by the order factors appear in ``_REASON_FACTOR_LABELS``
    — prevents day-to-day chip shuffling when two factors score identically.
    """
    scored: list[tuple[int, float, str]] = []
    for idx, (col, _label) in enumerate(_REASON_FACTOR_LABELS.items()):
        val = factor_scores.get(col)
        if val is None:
            continue
        try:
            scored.append((idx, float(val), col))
        except (TypeError, ValueError):
            continue

    # Sort by score desc, with declaration order as tie-breaker (ascending).
    scored.sort(key=lambda item: (-item[1], item[0]))

    chips: list[dict] = []
    for _idx, score, col in scored[:top_n]:
        chips.append({
            "factor": col,
            "label": _REASON_FACTOR_LABELS[col],
            "value": round(score, 2),
        })
    return chips


def _capped_target_source(source: Optional[str]) -> str:
    if source == _TARGET_SOURCE_DCF:
        return _TARGET_SOURCE_DCF_CAPPED
    if source == _TARGET_SOURCE_SCORE:
        return _TARGET_SOURCE_SCORE_CAPPED
    return source or _TARGET_SOURCE_SCORE_CAPPED


def get_selection_target_count(config_path: Optional[Path] = None) -> int:
    """Return the configured target portfolio size.

    Falls back to the module default when the YAML file is unavailable or
    malformed.
    """
    path = config_path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        return _PICKS_PER_PORTFOLIO
    try:
        with path.open("r", encoding="utf-8") as fh:
            full = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return _PICKS_PER_PORTFOLIO
    try:
        return int(full.get("selection", {}).get("target_count", _PICKS_PER_PORTFOLIO))
    except (TypeError, ValueError):
        return _PICKS_PER_PORTFOLIO


class PortfolioSelector:
    """Selects the top stocks per portfolio with constraint enforcement.

    Args:
        scoring_date: Last completed session allowed for scoring and model
            reference-price lookups. Defaults to today for legacy callers.
        selection_date: Effective/trade session. Defaults to ``scoring_date``;
            pre-open production uses T-1 for scoring and T here.
        config_path: Path to thresholds.yaml. Defaults to config/thresholds.yaml.
    """

    def __init__(
        self,
        scoring_date: Optional[date] = None,
        config_path: Optional[Path] = None,
        strategy_variant: str = _DEFAULT_STRATEGY_VARIANT,
        selection_overrides: Optional[dict] = None,
        selection_date: Optional[date] = None,
    ) -> None:
        self.scoring_date: date = scoring_date or date.today()
        self.selection_date: date = selection_date or self.scoring_date
        if self.scoring_date > self.selection_date:
            raise ValueError("scoring_date cannot be after selection_date")
        self._universe = UniverseBuilder(scoring_date=self.scoring_date)
        self._cfg = self._load_selection_config(config_path or _DEFAULT_CONFIG_PATH)
        if selection_overrides:
            self._cfg = self._merge_overrides(self._cfg, selection_overrides)
        self.strategy_variant = self._normalize_strategy_variant(strategy_variant)

    @staticmethod
    def _merge_overrides(cfg: dict, overrides: dict) -> dict:
        """Merge calibration overrides over the YAML selection config.

        One level of nested dicts (e.g. ``index_aware``) is merged rather
        than replaced so a single sub-key override keeps its siblings.
        """
        merged = dict(cfg)
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _load_selection_config(config_path: Path) -> dict:
        """Load the 'selection' section from thresholds.yaml."""
        if not config_path.exists():
            logger.warning("thresholds.yaml not found at %s; using defaults", config_path)
            return {}
        with config_path.open("r", encoding="utf-8") as fh:
            full = yaml.safe_load(fh) or {}
        return full.get("selection", {})

    @staticmethod
    def _normalize_strategy_variant(strategy_variant: str) -> str:
        """Return normalized portfolio strategy variant name."""
        normalized = (strategy_variant or _DEFAULT_STRATEGY_VARIANT).strip().lower()
        normalized = normalized.replace("-", "_")
        if normalized not in _VALID_STRATEGY_VARIANTS:
            raise ValueError(
                f"Unknown strategy_variant {strategy_variant!r}. "
                f"Valid: {sorted(_VALID_STRATEGY_VARIANTS)}"
            )
        return normalized

    def _index_aware_config(self) -> dict:
        """Return the nested selection.index_aware config block."""
        cfg = self._cfg.get("index_aware", {}) if isinstance(self._cfg, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _index_aware_float(self, key: str, default: float) -> float:
        try:
            return float(self._index_aware_config().get(key, default))
        except (TypeError, ValueError):
            return default

    def _index_aware_int(self, key: str, default: int) -> int:
        try:
            return int(self._index_aware_config().get(key, default))
        except (TypeError, ValueError):
            return default

    def _target_count(self) -> int:
        """Return the configured portfolio size for the current selector."""
        try:
            return int(self._cfg.get("target_count", _PICKS_PER_PORTFOLIO))
        except (TypeError, ValueError):
            return _PICKS_PER_PORTFOLIO

    def _max_per_sector(self) -> int:
        """Return the configured sector cap for the current selector."""
        try:
            return int(self._cfg.get("max_per_sector", _MAX_PER_SECTOR))
        except (TypeError, ValueError):
            return _MAX_PER_SECTOR

    def _max_banks(self) -> int:
        """Return the configured bank cap for the current selector."""
        try:
            return int(self._cfg.get("max_banks_per_portfolio", _MAX_BANKS))
        except (TypeError, ValueError):
            return _MAX_BANKS

    # ── Public API ────────────────────────────────────────────────────────────

    def select(
        self,
        portfolio: str,
        session: Session,
        current_holdings: Optional[list[int]] = None,
        exclude_tickers: Optional[set[str]] = None,
    ) -> list[dict]:
        """Select the top picks for *portfolio*.

        Args:
            portfolio: One of 'ALPHA', 'BETA', 'DELTA' (case-insensitive).
            session: Active SQLAlchemy session.
            current_holdings: Optional list of company_ids currently in the
                portfolio (used for turnover-penalty calculation).

        Returns:
            List of up to the configured target count, each with keys:
              company_id, ticker, score, rank, target_price, stop_loss, entry_price
        """
        portfolio = portfolio.upper()
        score_col = _PORTFOLIO_SCORE_COL.get(portfolio)
        if score_col is None:
            raise ValueError(
                f"Unknown portfolio {portfolio!r}. Valid: {sorted(_PORTFOLIO_SCORE_COL)}"
            )

        # Step 1: eligible universe
        if self.strategy_variant == _STRATEGY_INDEX_AWARE and portfolio == "ALPHA":
            universe_ids = self._get_index_aware_universe_ids(session)
        else:
            universe_ids = set(self._universe.get_universe(portfolio, session))
        if not universe_ids:
            logger.warning("Empty universe for %s — returning no picks", portfolio)
            return []

        # Step 2: fetch scored candidates for universe companies
        candidates = self._fetch_candidates(universe_ids, score_col, session)
        if not candidates:
            logger.warning("No scored candidates found for %s", portfolio)
            return []

        if exclude_tickers:
            candidates = [c for c in candidates if c["ticker"] not in exclude_tickers]

        if self.strategy_variant == _STRATEGY_INDEX_AWARE:
            candidates = self._apply_index_aware_scores(candidates, session)

        # Step 3: sort descending by composite score (None treated as -1)
        candidates.sort(key=lambda c: c["score"] if c["score"] is not None else -1.0, reverse=True)

        if self.strategy_variant == _STRATEGY_INDEX_AWARE and portfolio == "ALPHA":
            return self._select_index_aware(candidates, current_holdings, session)

        # Build lookup structures
        score_by_id = {c["company_id"]: (c["score"] or 0.0) for c in candidates}
        current_holdings_set = set(current_holdings or [])

        turnover_top_n = int(self._cfg.get("turnover_top_n", _TOP_N_FOR_TURNOVER))
        turnover_threshold = 1.0 + float(
            self._cfg.get("turnover_threshold", _TURNOVER_THRESHOLD - 1.0)
        )
        protected_window_ids = {c["company_id"] for c in candidates[:turnover_top_n]}
        target_count = self._target_count()

        # Step 6: determine which incumbents receive turnover protection
        # An incumbent is protected when it is in the configured top-N and
        # the best new candidate does not exceed its score by the turnover
        # threshold.
        new_candidates_scores = [
            (c["score"] or 0.0)
            for c in candidates
            if c["company_id"] not in current_holdings_set
        ]
        best_new_score = max(new_candidates_scores) if new_candidates_scores else 0.0


        protected_incumbents: set[int] = set()
        for cid in current_holdings_set:
            if cid not in protected_window_ids or cid not in score_by_id:
                continue
            incumbent_score = score_by_id[cid]
            if best_new_score < incumbent_score * turnover_threshold:
                protected_incumbents.add(cid)

        # Split candidates into incumbents-to-keep and new picks
        incumbent_cands = [c for c in candidates if c["company_id"] in protected_incumbents]
        new_cands = [c for c in candidates if c["company_id"] not in protected_incumbents]

        # Steps 4-7: two-pass selection (incumbents first, then new)
        picks: list[dict] = []
        sector_counts: dict[str, int] = {}
        bank_count: int = 0

        for candidate in incumbent_cands:
            if len(picks) >= target_count:
                break
            if self._violates_constraints(candidate, sector_counts, bank_count):
                continue
            pick = self._build_pick(candidate, len(picks) + 1, session)
            if pick is None:
                continue
            picks.append(pick)
            self._update_counters(candidate, sector_counts)
            if candidate["company_type"] == "BANK":
                bank_count += 1

        for candidate in new_cands:
            if len(picks) >= target_count:
                break
            if self._violates_constraints(candidate, sector_counts, bank_count):
                continue
            pick = self._build_pick(candidate, len(picks) + 1, session)
            if pick is None:
                continue
            picks.append(pick)
            self._update_counters(candidate, sector_counts)
            if candidate["company_type"] == "BANK":
                bank_count += 1

        # Step 8: correlation filter — swap correlated picks with next-best
        max_corr = self._cfg.get("max_correlation", _MAX_CORRELATION)
        picks = self._reduce_correlation(
            picks, new_cands, sector_counts, bank_count, max_corr, session
        )

        logger.info(
            "Selected %d picks for %s: %s",
            len(picks),
            portfolio,
            [p["ticker"] for p in picks],
        )
        return picks

    def select_all(self, session: Session) -> dict:
        """Select picks for all three portfolios.

        Returns:
            Dict with lowercase keys 'alpha', 'beta', 'delta', each mapping
            to a list of pick dicts (see select()).
        """
        results: dict = {}
        # Load tickers on cooldown (STOP_LOSS within last 4 weeks)
        cooldown_tickers = self._load_cooldown_tickers(session)
        if cooldown_tickers:
            logger.info("Excluding tickers on cooldown: %s", cooldown_tickers)

        # NOTE: BETA and DELTA commented out — only ALPHA portfolio is active.
        # To re-enable, uncomment "BETA" and "DELTA" in the tuple below.
        for portfolio in ("ALPHA",):  # "BETA", "DELTA"):
            current_holdings = self._load_current_holdings(portfolio, session)
            try:
                results[portfolio.lower()] = self.select(
                    portfolio, 
                    session, 
                    current_holdings, 
                    exclude_tickers=cooldown_tickers
                )
            except Exception as exc:
                logger.error("Portfolio %s selection failed: %s", portfolio, exc, exc_info=True)
                results[portfolio.lower()] = []
        return results

    def select_and_store(
        self,
        session: Session,
        cash_signal: Optional[CashSignalResult] = None,
    ) -> dict:
        """Select for all portfolios and persist results to portfolio_selections.

        Existing rows for the same portfolio + selection_date + company_id are
        updated in place; new rows are inserted.

        Monthly Rebalancing Logic:
          Before storing new picks, all existing open positions (exit_date IS NULL)
          for the active portfolios are marked as 'exited' with the current close
          price and scoring_date as exit_date. Then, new picks are stored as
          open positions. This ensures "Open Positions" only reflects the
          most recent month's selection.

        Phase 4 — Cash allocation:
          A :class:`CashSignalResult` may be supplied explicitly. When omitted
          the selector evaluates the signal itself (preserving backwards
          compatibility for callers that don't know about cash state). The
          returned ``cash_pct`` scales every pick weight so
          ``sum(weights) == 1 - cash_pct``.

        Returns:
            Same dict as select_all(); each pick additionally carries a
            ``weight`` float.
        """
        # Resolve the cash signal first so it can be stamped on every pick row.
        if cash_signal is None:
            cash_signal = CashSignalCalculator().compute(session, self.scoring_date)

        all_picks = self.select_all(session)

        # Safety net: enforce max_target_multiple cap
        max_multiple = self._cfg.get("max_target_multiple", _DEFAULT_MAX_TARGET_MULTIPLE)
        for picks in all_picks.values():
            for pick in picks:
                entry = pick.get("entry_price")
                target = pick.get("target_price")
                if entry and target and target > entry * max_multiple:
                    pick["target_price"] = round(entry * max_multiple, 2)
                    pick["target_source"] = _capped_target_source(
                        pick.get("target_source")
                    )

        # Apply cash-scaled weights: (1 - cash_pct) is the total equity budget,
        # split equally among the surviving picks.
        invested_fraction = max(0.0, 1.0 - cash_signal.cash_pct)
        for picks in all_picks.values():
            if not picks:
                continue
            per_pick_weight = invested_fraction / len(picks)
            for pick in picks:
                pick["weight"] = per_pick_weight
                pick["cash_state"] = cash_signal.state
                pick["cash_pct"] = cash_signal.cash_pct

        for portfolio_key, picks in all_picks.items():
            p_upper = portfolio_key.upper()

            # An empty pick list here can only mean a failure upstream (empty
            # universe, no ScoringResult rows for the date, or an exception
            # swallowed by select_all) — a deliberate "sell everything" is
            # expressed through cash_pct, never through zero picks. Exiting
            # the open positions in that case would liquidate the whole
            # portfolio on a transient glitch and pollute the P&L history
            # with fake REBALANCE exits, so keep the previous selection.
            if not picks:
                logger.error(
                    "No picks produced for %s on %s — keeping existing open "
                    "positions untouched (guard against transient failures)",
                    p_upper,
                    self.scoring_date,
                )
                continue

            current_ids = {pick["company_id"] for pick in picks}
            picks_by_company = {pick["company_id"]: pick for pick in picks}

            # --- 1. Position continuity (B1, 2026-07-05) ---
            # Only positions that actually LEFT the selection are exited.
            # Kept incumbents stay in their original row: entry_price and
            # selection_date preserve the real cost basis, so stop-loss and
            # P&L anchor to the true entry instead of resetting every
            # rotation. Rotation-scoped fields (score/target/weight/cycle
            # reference) are refreshed in place; the stop only ratchets UP.
            open_positions = (
                session.query(PortfolioSelection)
                .filter(
                    PortfolioSelection.portfolio == p_upper,
                    PortfolioSelection.exit_date.is_(None),
                    PortfolioSelection.selection_date < self.selection_date
                )
                .all()
            )

            continued_ids: set[int] = set()
            for pos in open_positions:
                pick = picks_by_company.get(pos.company_id)
                if pick is not None:
                    continued_ids.add(pos.company_id)
                    reason_chips = pick.get("reason_top_factors") or []
                    pos.composite_score = pick["score"]
                    pos.target_price = pick["target_price"]
                    pos.target_source = pick.get("target_source")
                    new_stop = pick["stop_loss"]
                    if new_stop is not None:
                        pos.stop_loss_price = max(
                            pos.stop_loss_price or 0.0, float(new_stop)
                        ) or None
                    pos.weight = pick.get("weight")
                    pos.cash_state = pick.get("cash_state")
                    pos.cash_pct = pick.get("cash_pct")
                    pos.reason_top_factors_json = (
                        json.dumps(reason_chips, separators=(",", ":"))
                        if reason_chips
                        else None
                    )
                    pos.cycle_ref_date = self.selection_date
                    pos.cycle_signal_date = self.scoring_date
                    pos.cycle_ref_price = pick["entry_price"]
                    logger.info(
                        "Continuing position %s (entry %s on %s preserved)",
                        pos.company_id,
                        f"{pos.entry_price:.2f}" if pos.entry_price is not None else "N/A",
                        pos.selection_date,
                    )
                    continue

                # Genuinely dropped from the selection — exit at current price.
                exit_price = self._get_latest_price(pos.company_id, session)
                pos.exit_date = self.selection_date
                pos.exit_price = exit_price or pos.entry_price
                if pos.selection_date is not None:
                    pos.holding_days = max(
                        0,
                        (self.selection_date - pos.selection_date).days,
                    )
                if (
                    pos.entry_price is not None
                    and pos.entry_price > 0
                    and pos.exit_price is not None
                ):
                    pos.return_pct = (
                        (pos.exit_price - pos.entry_price) / pos.entry_price
                    ) * 100.0
                # If exited via normal rebalance, set reason to REBALANCE
                if not pos.exit_reason:
                    pos.exit_reason = "REBALANCE"

                logger.info(
                    "Exiting old position: %s at %s",
                    pos.company_id,
                    f"{pos.exit_price:.2f}" if pos.exit_price is not None else "N/A",
                )

            # --- 1b. Re-running on the same day should replace the snapshot ---
            # Same-day rows are working-state artifacts, not historical rebalances.
            # Keep at most one row per selected company and delete stale leftovers.
            same_day_rows = (
                session.query(PortfolioSelection)
                .filter(
                    PortfolioSelection.portfolio == p_upper,
                    PortfolioSelection.selection_date == self.selection_date,
                )
                .order_by(PortfolioSelection.id)
                .all()
            )
            same_day_by_company: dict[int, list[PortfolioSelection]] = {}
            for row in same_day_rows:
                same_day_by_company.setdefault(row.company_id, []).append(row)

            for company_id, rows in same_day_by_company.items():
                if company_id not in current_ids:
                    for row in rows:
                        session.delete(row)
                    continue
                for duplicate in rows[1:]:
                    session.delete(duplicate)

            # --- 2. Store New Picks (continued incumbents already updated) ---
            for pick in picks:
                if pick["company_id"] in continued_ids:
                    continue
                existing = (
                    session.query(PortfolioSelection)
                    .filter_by(
                        portfolio=p_upper,
                        selection_date=self.selection_date,
                        company_id=pick["company_id"],
                    )
                    .first()
                )
                # Phase 5: serialise reason chips once per pick.
                reason_chips = pick.get("reason_top_factors") or []
                reason_json = (
                    json.dumps(reason_chips, separators=(",", ":"))
                    if reason_chips
                    else None
                )
                if existing:
                    existing.composite_score = pick["score"]
                    existing.target_price = pick["target_price"]
                    existing.target_source = pick.get("target_source")
                    existing.stop_loss_price = pick["stop_loss"]
                    existing.entry_price = pick["entry_price"]
                    existing.weight = pick.get("weight")
                    existing.cash_state = pick.get("cash_state")
                    existing.cash_pct = pick.get("cash_pct")
                    existing.reason_top_factors_json = reason_json
                    existing.signal_date = self.scoring_date
                    existing.cycle_ref_date = self.selection_date
                    existing.cycle_signal_date = self.scoring_date
                    existing.cycle_ref_price = pick["entry_price"]
                    existing.highest_close = pick["entry_price"]
                    # A same-day rerun may re-pick a name exited as REBALANCE
                    # earlier the same day — that exit was working state, not
                    # a real trade, so reopen the row.
                    if (
                        existing.exit_date == self.selection_date
                        and existing.exit_reason == "REBALANCE"
                    ):
                        existing.exit_date = None
                        existing.exit_price = None
                        existing.exit_reason = None
                        existing.return_pct = None
                        existing.holding_days = None
                else:
                    session.add(
                        PortfolioSelection(
                            portfolio=p_upper,
                            selection_date=self.selection_date,
                            signal_date=self.scoring_date,
                            company_id=pick["company_id"],
                            entry_price=pick["entry_price"],
                            composite_score=pick["score"],
                            target_price=pick["target_price"],
                            target_source=pick.get("target_source"),
                            stop_loss_price=pick["stop_loss"],
                            weight=pick.get("weight"),
                            cash_state=pick.get("cash_state"),
                            cash_pct=pick.get("cash_pct"),
                            reason_top_factors_json=reason_json,
                            cycle_ref_date=self.selection_date,
                            cycle_signal_date=self.scoring_date,
                            cycle_ref_price=pick["entry_price"],
                            highest_close=pick["entry_price"],
                        )
                    )

            # A previously-open row that was resurrected in a same-day rerun
            # but exited by continuity logic above stays exited — that is the
            # final state for the day. Now record this rotation's reference
            # marks for every position that is open after the rebalance.
            self._upsert_cycle_marks(session, p_upper, picks)

        session.commit()
        logger.info(
            "Stored portfolio selections effective %s from signal %s",
            self.selection_date,
            self.scoring_date,
        )
        return all_picks

    def _upsert_cycle_marks(
        self, session: Session, portfolio: str, picks: list[dict]
    ) -> None:
        """Record this rotation's per-position reference prices (B1).

        One row per (portfolio, cycle_date, company_id); reruns on the same
        day update the price in place. The PWA uses these marks to compute
        completed-period returns for positions that never exited.
        """
        from us_picker.db.schema import PortfolioCycleMark

        # Rerun cleanup: drop marks for names no longer in today's selection.
        pick_ids = {pick["company_id"] for pick in picks}
        stale_marks = (
            session.query(PortfolioCycleMark)
            .filter_by(portfolio=portfolio, cycle_date=self.selection_date)
            .all()
        )
        for mark in stale_marks:
            if mark.company_id not in pick_ids:
                session.delete(mark)

        for pick in picks:
            mark = (
                session.query(PortfolioCycleMark)
                .filter_by(
                    portfolio=portfolio,
                    cycle_date=self.selection_date,
                    company_id=pick["company_id"],
                )
                .first()
            )
            if mark:
                mark.ref_price = pick["entry_price"]
                mark.signal_date = self.scoring_date
            else:
                session.add(
                    PortfolioCycleMark(
                        portfolio=portfolio,
                        cycle_date=self.selection_date,
                        signal_date=self.scoring_date,
                        company_id=pick["company_id"],
                        ref_price=pick["entry_price"],
                    )
                )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get_index_aware_universe_ids(self, session: Session) -> set[int]:
        """Return the V2 candidate universe.

        The core remains the production ALPHA universe. In addition, V2 allows
        a tightly filtered non-core sleeve so BIST100 banks/holdings/REITs can
        enter when relative strength confirms them. This is deliberately kept
        separate from the classic universe; ``index_aware`` is the main
        portfolio model, while ``classic`` remains available for comparison.
        """
        core_ids = set(self._universe.get_universe("ALPHA", session))
        avg_volumes = self._average_turnover_by_company(session)
        min_volume = self._index_aware_float(
            "min_avg_volume_try", _INDEX_AWARE_MIN_AVG_VOLUME_TRY
        )
        min_free_float = self._index_aware_float(
            "min_free_float", _INDEX_AWARE_MIN_FREE_FLOAT
        )
        min_data = self._index_aware_float(
            "min_data_completeness", _INDEX_AWARE_MIN_DATA_COMPLETENESS
        )

        extra_types = {"BANK", "FINANCIAL", "HOLDING", "REIT", "INSURANCE"}
        rows = (
            session.query(ScoringResult, Company)
            .join(Company, Company.id == ScoringResult.company_id)
            .filter(
                ScoringResult.scoring_date == self.scoring_date,
                ScoringResult.composite_alpha.isnot(None),
                Company.is_active.is_(True),
            )
            .all()
        )

        extra_ids: set[int] = set()
        for score, company in rows:
            company_type = (company.company_type or "").upper()
            if company_type not in extra_types:
                continue
            if score.risk_tier == "HIGH":
                continue
            if company.free_float_pct is None or company.free_float_pct < min_free_float:
                continue
            if (score.data_completeness or 0.0) < min_data:
                continue
            if avg_volumes.get(company.id, 0.0) < min_volume:
                continue
            extra_ids.add(company.id)

        return core_ids | extra_ids

    def _average_turnover_by_company(self, session: Session) -> dict[int, float]:
        """Return trailing average TRY turnover for the selector lookback."""
        lookback_days = self._index_aware_int("liquidity_lookback_days", 30)
        cutoff = self.scoring_date - timedelta(days=lookback_days)

        turnover_expr = case(
            (DailyPrice.source.ilike("YAHOO%"), DailyPrice.close * DailyPrice.volume),
            else_=DailyPrice.volume,
        )

        rows = (
            session.query(
                DailyPrice.company_id,
                func.avg(turnover_expr).label("avg_turnover"),
            )
            .filter(DailyPrice.date >= cutoff)
            .filter(DailyPrice.date <= self.scoring_date)
            .filter(DailyPrice.close.isnot(None))
            .filter(DailyPrice.volume.isnot(None))
            .group_by(DailyPrice.company_id)
            .all()
        )
        return {int(row.company_id): float(row.avg_turnover or 0.0) for row in rows}

    def _apply_index_aware_scores(
        self,
        candidates: list[dict],
        session: Session,
    ) -> list[dict]:
        """Blend model score with XU100-relative strength for V2 selection."""
        model_weight = self._index_aware_float(
            "model_weight", _INDEX_AWARE_MODEL_WEIGHT
        )
        relative_weight = self._index_aware_float(
            "relative_strength_weight", _INDEX_AWARE_RELATIVE_WEIGHT
        )
        technical_weight = self._index_aware_float(
            "technical_weight", _INDEX_AWARE_TECHNICAL_WEIGHT
        )
        total_weight = model_weight + relative_weight + technical_weight
        if total_weight <= 0:
            model_weight, relative_weight, technical_weight = (
                _INDEX_AWARE_MODEL_WEIGHT,
                _INDEX_AWARE_RELATIVE_WEIGHT,
                _INDEX_AWARE_TECHNICAL_WEIGHT,
            )
            total_weight = model_weight + relative_weight + technical_weight

        adjusted: list[dict] = []
        for candidate in candidates:
            row = dict(candidate)
            base_score = float(row.get("score") or 0.0)
            rel_score = self._relative_strength_score(row["ticker"], session)
            tech_score = float(row.get("technical_score") or 50.0)
            blended = (
                (base_score * model_weight)
                + (rel_score * relative_weight)
                + (tech_score * technical_weight)
            ) / total_weight
            row["base_score"] = base_score
            row["relative_strength_score"] = rel_score
            row["score"] = round(blended, 4)
            adjusted.append(row)
        return adjusted

    def _relative_strength_score(self, ticker: str, session: Session) -> float:
        """Return 0-100 score for 3-month performance vs XU100."""
        lookback_days = self._index_aware_int(
            "relative_lookback_days", _RELATIVE_STRENGTH_LOOKBACK_DAYS
        )
        start_date = self.scoring_date - timedelta(days=lookback_days)
        p0 = self._get_price_by_ticker(ticker, start_date, session)
        p1 = self._get_price_by_ticker(ticker, self.scoring_date, session)
        x0 = self._get_price_by_ticker("XU100", start_date, session)
        x1 = self._get_price_by_ticker("XU100", self.scoring_date, session)
        if p0 <= 0 or p1 <= 0 or x0 <= 0 or x1 <= 0:
            return 50.0

        rel_pp = ((p1 / p0) - (x1 / x0)) * 100.0
        scaled = 50.0 + rel_pp * 2.0
        return max(0.0, min(100.0, scaled))

    def _get_price_by_ticker(
        self,
        ticker: str,
        target_date: date,
        session: Session,
    ) -> float:
        """Return latest adjusted close/close for ticker on or before target_date."""
        return get_spliced_price_by_ticker(session, ticker, target_date)

    def _select_index_aware(
        self,
        candidates: list[dict],
        current_holdings: Optional[list[int]],
        session: Session,
    ) -> list[dict]:
        """Select V2 portfolio using BIST-relative strength and sleeve caps."""
        target_count = self._target_count()
        current_holdings_set = set(current_holdings or [])
        min_bist100 = self._index_aware_int("min_bist100", _INDEX_AWARE_MIN_BIST100)
        max_non_bist100 = self._index_aware_int(
            "max_non_bist100", _INDEX_AWARE_MAX_NON_BIST100
        )
        required_bist100 = max(min_bist100, target_count - max_non_bist100)
        available_bist100 = sum(
            1 for candidate in candidates if candidate.get("is_bist100")
        )
        if available_bist100 < required_bist100:
            logger.error(
                "Index-aware selection cannot fill %d slots: only %d BIST100 "
                "candidates are available but at least %d are required; "
                "returning no picks so the existing portfolio is preserved",
                target_count,
                available_bist100,
                required_bist100,
            )
            return []
        incumbent_window = self._index_aware_int(
            "incumbent_window", _INDEX_AWARE_INCUMBENT_WINDOW
        )
        incumbent_min_score = self._index_aware_float(
            "incumbent_min_score", _INDEX_AWARE_INCUMBENT_MIN_SCORE
        )

        picks: list[dict] = []
        selected_candidates: list[dict] = []
        sector_counts: dict[str, int] = {}
        bank_count = 0

        def try_add(candidate: dict) -> bool:
            nonlocal bank_count
            if len(picks) >= target_count:
                return False
            if any(p["company_id"] == candidate["company_id"] for p in picks):
                return False
            if self._violates_constraints(candidate, sector_counts, bank_count):
                return False
            if self._violates_index_aware_constraints(candidate, selected_candidates):
                return False
            pick = self._build_pick(candidate, len(picks) + 1, session)
            if pick is None:
                return False
            picks.append(pick)
            selected_candidates.append(candidate)
            self._update_counters(candidate, sector_counts)
            if self._is_bank(candidate):
                bank_count += 1
            return True

        # 1) Keep strong incumbents first to control unnecessary turnover.
        # The quality bar is checked on base_score (the model composite,
        # 0-100 percentile), not the blended score: blending pulls everything
        # toward the 50-85 band (a composite-99 stock that merely tracks
        # XU100 blends to ~82), so an 88 threshold on the blended scale
        # almost never fired and the protection was effectively dead.
        for candidate in candidates[:incumbent_window]:
            if (
                candidate["company_id"] in current_holdings_set
                and (candidate.get("base_score") or 0.0) >= incumbent_min_score
            ):
                try_add(candidate)

        # 2) Ensure a liquid/index core before filling smaller alpha names.
        for candidate in candidates:
            if sum(1 for p in selected_candidates if p.get("is_bist100")) >= min_bist100:
                break
            if candidate.get("is_bist100"):
                try_add(candidate)

        # 3) Prefer one index financial/holding sleeve when it scores well.
        if not any(self._is_financial_or_holding_sleeve(p) for p in selected_candidates):
            for candidate in candidates:
                if (
                    candidate.get("is_bist100")
                    and self._is_financial_or_holding_sleeve(candidate)
                    and self._passes_financial_sleeve_quality(candidate)
                ):
                    if try_add(candidate):
                        break

        # 4) Fill remaining slots by the blended score.
        for candidate in candidates:
            try_add(candidate)
            if len(picks) >= target_count:
                break

        # 5) Correlation filter — parity with the classic path. Before
        # 2026-07-02 the index-aware early return in select() skipped
        # _reduce_correlation entirely, so the documented 0.70/120d pairwise
        # cap was silently not enforced on production picks.
        max_corr = self._cfg.get("max_correlation", _MAX_CORRELATION)
        picked_ids = {p["company_id"] for p in picks}
        remaining = [c for c in candidates if c["company_id"] not in picked_ids]
        picks = self._reduce_correlation(
            picks,
            remaining,
            sector_counts,
            bank_count,
            max_corr,
            session,
            index_aware=True,
        )

        logger.info(
            "Selected %d index-aware picks for ALPHA: %s",
            len(picks),
            [p["ticker"] for p in picks],
        )
        return picks

    def _violates_index_aware_constraints(
        self,
        candidate: dict,
        selected_candidates: list[dict],
    ) -> bool:
        """Return True when V2 BIST/non-BIST sleeve caps would be breached."""
        max_non_bist100 = self._index_aware_int(
            "max_non_bist100", _INDEX_AWARE_MAX_NON_BIST100
        )
        if not candidate.get("is_bist100"):
            non_bist_count = sum(1 for p in selected_candidates if not p.get("is_bist100"))
            if non_bist_count >= max_non_bist100:
                logger.debug(
                    "Skip %s: non-BIST100 cap reached (%d)",
                    candidate["ticker"],
                    max_non_bist100,
                )
                return True
        return False

    @staticmethod
    def _is_financial_or_holding_sleeve(candidate: dict) -> bool:
        """Return True for the tactical BIST financial/holding sleeve."""
        return (candidate.get("company_type") or "").upper() in {
            "BANK",
            "FINANCIAL",
            "INSURANCE",
            "HOLDING",
        }

    def _passes_financial_sleeve_quality(self, candidate: dict) -> bool:
        """Return True when a forced financial/holding sleeve is strong enough."""
        min_score = self._index_aware_float(
            "financial_sleeve_min_score",
            _INDEX_AWARE_FINANCIAL_SLEEVE_MIN_SCORE,
        )
        min_relative = self._index_aware_float(
            "financial_sleeve_min_relative_strength",
            _INDEX_AWARE_FINANCIAL_SLEEVE_MIN_RELATIVE,
        )
        try:
            score = float(candidate.get("score") or 0.0)
        except (TypeError, ValueError):
            return False
        try:
            relative_score = float(candidate.get("relative_strength_score", 50.0))
        except (TypeError, ValueError):
            relative_score = 50.0
        return score >= min_score and relative_score >= min_relative

    def _load_current_holdings(self, portfolio: str, session: Session) -> list[int]:
        """Return company_ids of open positions from the most recent prior selection.

        Used to activate the turnover penalty — incumbents are retained when
        they are still in the top-10 and no challenger beats them by >15%.
        """
        latest_date = (
            session.query(func.max(PortfolioSelection.selection_date))
            .filter(
                PortfolioSelection.portfolio == portfolio,
                PortfolioSelection.selection_date <= self.selection_date,
                PortfolioSelection.exit_date.is_(None),
            )
            .scalar()
        )
        if not latest_date:
            return []
        rows = (
            session.query(PortfolioSelection.company_id)
            .filter(
                PortfolioSelection.portfolio == portfolio,
                PortfolioSelection.selection_date == latest_date,
                PortfolioSelection.exit_date.is_(None),
            )
            .all()
        )
        return [r[0] for r in rows]

    def _load_cooldown_tickers(self, session: Session) -> set[str]:
        """Return tickers on cooldown (STOP_LOSS exit within last 4 weeks)."""
        # A ticker is on cooldown if it was exited for STOP_LOSS within the last 4 weeks.
        cooldown_weeks = int(self._cfg.get("cooldown_weeks", 4))
        cooldown_cutoff = self.scoring_date - timedelta(weeks=cooldown_weeks)
        
        rows = (
            session.query(Company.ticker)
            .join(PortfolioSelection, PortfolioSelection.company_id == Company.id)
            .filter(
                PortfolioSelection.exit_reason == "STOP_LOSS",
                PortfolioSelection.exit_date >= cooldown_cutoff
            )
            .distinct()
            .all()
        )
        return {r[0] for r in rows}

    def _fetch_candidates(
        self, universe_ids: set[int], score_col: str, session: Session
    ) -> list[dict]:
        """Fetch latest ScoringResult + Company data for all companies in *universe_ids*.

        Returns a list of candidate dicts with keys:
          company_id, ticker, score, sector_custom, company_type, dcf_mos
        """
        # Strictly use the configured scoring_date
        rows = (
            session.query(ScoringResult, Company)
            .filter(
                ScoringResult.company_id.in_(universe_ids),
                ScoringResult.scoring_date == self.scoring_date,
            )
            .join(Company, Company.id == ScoringResult.company_id)
            .all()
        )

        candidates = []
        for score_row, company in rows:
            composite = getattr(score_row, score_col, None)
            # Phase 5: snapshot the full normalized factor score row so the
            # selector can derive "why selected" chips without another query.
            factor_scores = {
                col: getattr(score_row, col, None)
                for col in _REASON_FACTOR_LABELS
            }
            candidates.append(
                {
                    "company_id": company.id,
                    "ticker": company.ticker,
                    "score": composite,
                    "base_score": composite,
                    "sector_custom": company.sector_custom,
                    "sector_key": company.sector_custom or company.sector_bist,
                    "company_type": company.company_type,
                    "is_bist100": bool(company.is_bist100),
                    "risk_tier": getattr(score_row, "risk_tier", None),
                    "free_float_pct": company.free_float_pct,
                    "dcf_mos": score_row.dcf_margin_of_safety_pct,
                    "data_completeness": getattr(score_row, "data_completeness", None),
                    "quality_flags_json": getattr(score_row, "quality_flags_json", None),
                    "technical_score": getattr(score_row, "technical_score", None),
                    "above_200ma": getattr(score_row, "above_200ma", None),
                    "relative_strength_score": None,
                    "factor_scores": factor_scores,
                }
            )
        return candidates

    def _violates_constraints(
        self,
        candidate: dict,
        sector_counts: dict[str, int],
        bank_count: int,
    ) -> bool:
        """Return True if selecting *candidate* would breach any portfolio constraint."""
        # 1. Falling Knife (Düşen Bıçak) Filter
        # Reject candidates whose technicals look bad in BOTH a relative
        # AND absolute sense. The percentile-only check used to be too
        # forgiving in bull markets (35th percentile of the universe can
        # still be a stock above its 200-day MA in a strong tape) and too
        # strict in bear markets (everything ranks low). Combining the
        # normalized score with the raw "price above 200-day SMA" signal
        # makes the filter scale with the actual trend, not just the
        # cross-section. (Audit MEDIUM #11, 2026-05-07.)
        min_tech = self._cfg.get("min_technical_score", 35.0)
        tech_score = candidate.get("technical_score")
        above_200ma = candidate.get("above_200ma")

        # Hard reject: low percentile AND below the long-term trend line.
        if (
            tech_score is not None
            and tech_score < min_tech
            and above_200ma is False
        ):
            logger.debug(
                "Skip %s: falling-knife (technical %.1f < %.1f AND below 200MA)",
                candidate["ticker"], tech_score, min_tech,
            )
            return True
        # Backward-compat soft reject: if we don't yet know above_200ma
        # (legacy DB rows), still apply the original percentile-only filter
        # so existing behavior is preserved for unmigrated data.
        if above_200ma is None and tech_score is not None and tech_score < min_tech:
            logger.debug(
                "Skip %s: falling-knife legacy (technical %.1f < %.1f, no 200MA data)",
                candidate["ticker"], tech_score, min_tech,
            )
            return True

        # 2. Sector Cap
        sector = self._sector_key(candidate)
        max_per_sector = self._max_per_sector()
        if sector is not None:
            if sector_counts.get(sector, 0) >= max_per_sector:
                logger.debug(
                    "Skip %s: sector %r already has %d picks",
                    candidate["ticker"],
                    sector,
                    max_per_sector,
                )
                return True

        # 3. Bank Cap
        if self._is_bank(candidate) and bank_count >= self._max_banks():
            logger.debug("Skip %s: bank limit reached", candidate["ticker"])
            return True

        return False

    def _reduce_correlation(
        self,
        picks: list[dict],
        remaining_candidates: list[dict],
        sector_counts: dict[str, int],
        bank_count: int,
        max_corr: float,
        session: Session,
        index_aware: bool = False,
    ) -> list[dict]:
        """Replace highly correlated picks with the next-best uncorrelated candidate.

        Uses trailing daily return correlation. If any pair exceeds *max_corr*,
        the lower-scored pick is replaced with the next candidate from
        *remaining_candidates* that doesn't violate constraints or correlation.

        Args:
            picks: Current list of selected picks (with company_id, ticker, score).
            remaining_candidates: Candidates not yet selected, sorted by score desc.
            sector_counts: Current sector counter dict.
            bank_count: Current bank count.
            max_corr: Maximum allowed pairwise correlation (e.g. 0.85).
            session: DB session for price lookups.
            index_aware: When True, replacements must also respect the V2
                non-BIST100 sleeve cap (checked against the picks that would
                remain after the swap).

        Returns:
            Updated picks list with correlated picks replaced.
        """
        if len(picks) < 2:
            return picks

        lookback = self._cfg.get("correlation_lookback_days", _CORRELATION_LOOKBACK)
        cutoff = self.scoring_date - timedelta(days=lookback)

        def _get_returns(company_id: int) -> Optional[np.ndarray]:
            """Fetch daily close prices and compute log returns."""
            rows = (
                session.query(DailyPrice.date, DailyPrice.close)
                .filter(
                    DailyPrice.company_id == company_id,
                    DailyPrice.date >= cutoff,
                    DailyPrice.date <= self.scoring_date,
                    DailyPrice.close.isnot(None),
                )
                .order_by(DailyPrice.date)
                .all()
            )
            if len(rows) < 30:  # Need at least 30 data points
                return None
            prices = np.array([r.close for r in rows], dtype=float)
            returns = np.diff(np.log(prices))  # log returns
            return returns

        # Pre-compute returns for all picks
        pick_returns: dict[int, Optional[np.ndarray]] = {}
        for pick in picks:
            pick_returns[pick["company_id"]] = _get_returns(pick["company_id"])

        # Check all pairs for high correlation
        replaced = True
        max_iterations = 5  # safety cap
        iteration = 0
        while replaced and iteration < max_iterations:
            replaced = False
            iteration += 1
            for i in range(len(picks)):
                for j in range(i + 1, len(picks)):
                    ret_i = pick_returns.get(picks[i]["company_id"])
                    ret_j = pick_returns.get(picks[j]["company_id"])
                    if ret_i is None or ret_j is None:
                        continue

                    # Align lengths (they should be close but might differ slightly)
                    min_len = min(len(ret_i), len(ret_j))
                    if min_len < 20:
                        continue
                    corr = np.corrcoef(ret_i[-min_len:], ret_j[-min_len:])[0, 1]

                    if np.isnan(corr) or corr <= max_corr:
                        continue

                    # High correlation detected — replace the lower-scored pick
                    if (picks[i].get("score") or 0) >= (picks[j].get("score") or 0):
                        drop_idx = j
                    else:
                        drop_idx = i

                    dropped = picks[drop_idx]
                    logger.info(
                        "Correlation %.2f between %s and %s exceeds %.2f — replacing %s",
                        corr,
                        picks[i]["ticker"],
                        picks[j]["ticker"],
                        max_corr,
                        dropped["ticker"],
                    )

                    # Find the next-best replacement from remaining candidates
                    pick_ids = {p["company_id"] for p in picks}
                    replacement = None
                    temp_sector_counts = dict(sector_counts)
                    self._decrement_counters(dropped, temp_sector_counts)
                    temp_bank_count = bank_count - (1 if self._is_bank(dropped) else 0)
                    for cand in remaining_candidates:
                        if cand["company_id"] in pick_ids:
                            continue
                        if self._violates_constraints(cand, temp_sector_counts, temp_bank_count):
                            continue
                        if index_aware:
                            survivors = [
                                p for k, p in enumerate(picks) if k != drop_idx
                            ]
                            if self._violates_index_aware_constraints(cand, survivors):
                                continue

                        # Check correlation of replacement against other picks
                        cand_ret = _get_returns(cand["company_id"])
                        ok = True
                        if cand_ret is not None:
                            for k, other_pick in enumerate(picks):
                                if k == drop_idx:
                                    continue
                                other_ret = pick_returns.get(other_pick["company_id"])
                                if other_ret is None:
                                    continue
                                ml = min(len(cand_ret), len(other_ret))
                                if ml < 20:
                                    continue
                                c = np.corrcoef(cand_ret[-ml:], other_ret[-ml:])[0, 1]
                                if not np.isnan(c) and c > max_corr:
                                    ok = False
                                    break
                        if ok:
                            replacement = cand
                            pick_returns[cand["company_id"]] = cand_ret
                            break

                    if replacement is not None:
                        new_pick = self._build_pick(
                            replacement, drop_idx + 1, session
                        )
                        if new_pick is None:
                            continue
                        self._decrement_counters(dropped, sector_counts)
                        if self._is_bank(dropped):
                            bank_count = max(0, bank_count - 1)
                        picks[drop_idx] = new_pick
                        self._update_counters(replacement, sector_counts)
                        if self._is_bank(replacement):
                            bank_count += 1
                        replaced = True
                        logger.info(
                            "Replaced with %s (score %.1f)",
                            replacement["ticker"],
                            replacement.get("score") or 0,
                        )
                        break  # restart pair checking
                    else:
                        logger.warning(
                            "No uncorrelated replacement found for %s — keeping it",
                            dropped["ticker"],
                        )
                if replaced:
                    break

        return picks

    def _update_counters(
        self, candidate: dict, sector_counts: dict[str, int]
    ) -> None:
        """Increment the sector pick counter for *candidate*."""
        sector = self._sector_key(candidate)
        if sector is not None:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    def _decrement_counters(
        self, candidate: dict, sector_counts: dict[str, int]
    ) -> None:
        """Decrement the sector pick counter for *candidate* when replacing it."""
        sector = self._sector_key(candidate)
        if sector is None or sector not in sector_counts:
            return
        next_count = sector_counts[sector] - 1
        if next_count > 0:
            sector_counts[sector] = next_count
        else:
            sector_counts.pop(sector, None)

    @staticmethod
    def _is_bank(candidate: dict) -> bool:
        """Apply the bank cap to both banks and banking-routed financials."""
        return (candidate.get("company_type") or "").upper() in {
            "BANK",
            "FINANCIAL",
            "INSURANCE",
        }

    @staticmethod
    def _sector_key(candidate: dict) -> Optional[str]:
        """Use the mapped custom sector when available, else fall back to BIST sector."""
        return candidate.get("sector_key") or candidate.get("sector_custom")

    def _build_pick(
        self, candidate: dict, rank: int, session: Session
    ) -> Optional[dict]:
        """Assemble the final pick dict including target price and stop loss."""
        entry = self._get_latest_price(candidate["company_id"], session)
        if entry is None:
            logger.debug(
                "Skip %s: no price available on or before %s",
                candidate["ticker"],
                self.scoring_date,
            )
            return None
        # Only pull ATR eagerly when the expected-move target needs it (#11);
        # otherwise the stop computes its own ATR lazily, preserving the exact
        # prior call graph (and monkeypatch-based tests).
        target_needs_atr = bool(
            (self._cfg.get("target_expected_move", {}) or {}).get("enabled")
        )
        atr = self._atr_value(candidate["company_id"], session) if target_needs_atr else None
        target, target_source = self._compute_target_price_with_source(
            candidate, entry, atr=atr
        )
        if atr is not None:
            stop = self._compute_atr_stop(candidate["company_id"], entry, session, atr=atr)
        else:
            # Default path: exact prior 3-arg call (stop computes its own ATR).
            stop = self._compute_atr_stop(candidate["company_id"], entry, session)

        reason_top_factors = _compute_reason_top_factors(
            candidate.get("factor_scores") or {}
        )

        return {
            "company_id": candidate["company_id"],
            "ticker": candidate["ticker"],
            "score": candidate["score"],
            "base_score": candidate.get("base_score", candidate.get("score")),
            "relative_strength_score": candidate.get("relative_strength_score"),
            "is_bist100": bool(candidate.get("is_bist100")),
            "company_type": candidate.get("company_type"),
            "rank": rank,
            "entry_price": entry,
            "target_price": target,
            "target_source": target_source,
            "stop_loss": stop,
            "dcf_mos": candidate.get("dcf_mos"),
            "quality_flags_json": candidate.get("quality_flags_json"),
            "reason_top_factors": reason_top_factors,
        }

    def _get_latest_price(
        self, company_id: int, session: Session
    ) -> Optional[float]:
        """Return the most recent adjusted_close (or close) for *company_id*."""
        row = (
            session.query(DailyPrice)
            .filter(
                DailyPrice.company_id == company_id,
                DailyPrice.adjusted_close.isnot(None),
                DailyPrice.date <= self.scoring_date,
            )
            .order_by(DailyPrice.date.desc())
            .first()
        )
        if row:
            return row.adjusted_close

        # Fallback: plain close
        row = (
            session.query(DailyPrice)
            .filter(
                DailyPrice.company_id == company_id,
                DailyPrice.close.isnot(None),
                DailyPrice.date <= self.scoring_date,
            )
            .order_by(DailyPrice.date.desc())
            .first()
        )
        return row.close if row else None

    def _compute_target_price(
        self, candidate: dict, entry_price: Optional[float]
    ) -> Optional[float]:
        """Backward-compatible price-only wrapper for tests and callers."""
        target, _source = self._compute_target_price_with_source(candidate, entry_price)
        return target

    def _compute_target_price_with_source(
        self, candidate: dict, entry_price: Optional[float], atr: Optional[float] = None
    ) -> tuple[Optional[float], Optional[str]]:
        """Derive the target price from DCF MoS, an ATR expected move, or score.

        DCF path:
          MoS% represents (intrinsic - price) / intrinsic.
          => intrinsic = price / (1 - MoS/100)

        ATR expected-move path (opt-in, #11):
          When ``selection.target_expected_move.enabled`` is true and a DCF MoS
          is unavailable, target = entry + mult × ATR — a volatility-scaled move
          rather than an arbitrary score×30% upside. Default OFF (unchanged).

        Score-implied fallback:
          Upside = max(10%, (score/100) * 30%).  A 100-score stock implies ~30% upside.
          Penalized by 30% if data_completeness < 60%.

        The result is capped at entry_price * max_target_multiple (configurable
        in thresholds.yaml selection.max_target_multiple, default 2.5).
        This prevents unrealistically large targets from very high DCF MoS values.
        """
        if entry_price is None:
            return None, None

        max_multiple = self._cfg.get("max_target_multiple", _DEFAULT_MAX_TARGET_MULTIPLE)
        max_target = entry_price * max_multiple

        mos = candidate.get("dcf_mos")
        if mos is not None and 0.0 < mos < 100.0:
            dcf_target = entry_price / (1.0 - mos / 100.0)
            source = (
                _TARGET_SOURCE_DCF_CAPPED
                if dcf_target > max_target
                else _TARGET_SOURCE_DCF
            )
            return round(min(dcf_target, max_target), 2), source

        # #11: opt-in ATR expected-move target for DCF-less names.
        tem_cfg = self._cfg.get("target_expected_move", {}) or {}
        if tem_cfg.get("enabled") and atr and atr > 0:
            mult = float(tem_cfg.get("atr_multiple", _DEFAULT_TARGET_ATR_MULT))
            atr_target = entry_price + mult * atr
            # Keep a sane floor of the score-implied minimum upside.
            atr_target = max(atr_target, entry_price * (1.0 + _MIN_UPSIDE))
            source = (
                _TARGET_SOURCE_ATR_CAPPED
                if atr_target > max_target
                else _TARGET_SOURCE_ATR
            )
            return round(min(atr_target, max_target), 2), source

        score = candidate.get("score")
        if score is None:
            score = 50.0

        # Improved heuristic: configurable max upside for a perfect 100-score stock.
        # Default is 30% upside for a 100-score stock.
        max_implied_upside = self._cfg.get("max_implied_upside", 0.30)
        upside = max(_MIN_UPSIDE, (score / 100.0) * max_implied_upside)

        # Penalize low data completeness — if we have little data, reduce
        # confidence in the target by cutting upside by 30%.
        # data_completeness is stored as a percentage on the 0-100 scale.
        completeness = candidate.get("data_completeness")
        if completeness is not None and completeness < 60.0:
            upside *= 0.70

        score_target = entry_price * (1.0 + upside)
        source = (
            _TARGET_SOURCE_SCORE_CAPPED
            if score_target > max_target
            else _TARGET_SOURCE_SCORE
        )
        return round(min(score_target, max_target), 2), source

    def _atr_value(self, company_id: int, session: Session) -> Optional[float]:
        """Raw ATR(period) as-of scoring_date, or None if insufficient data.

        Shared by the stop-loss and the (opt-in) ATR expected-move target so
        both read the same volatility estimate from a single OHLC pull.
        """
        atr_period = self._cfg.get("atr_period", _ATR_PERIOD)
        cutoff = self.scoring_date - timedelta(days=atr_period * 3)  # extra margin
        rows = (
            session.query(DailyPrice.high, DailyPrice.low, DailyPrice.close)
            .filter(
                DailyPrice.company_id == company_id,
                DailyPrice.date >= cutoff,
                DailyPrice.date <= self.scoring_date,
                DailyPrice.high.isnot(None),
                DailyPrice.low.isnot(None),
                DailyPrice.close.isnot(None),
            )
            .order_by(DailyPrice.date)
            .all()
        )
        if len(rows) < atr_period + 1:
            return None
        true_ranges: list[float] = []
        for i in range(1, len(rows)):
            high = rows[i].high
            low = rows[i].low
            prev_close = rows[i - 1].close
            true_ranges.append(
                max(high - low, abs(high - prev_close), abs(low - prev_close))
            )
        return sum(true_ranges[-atr_period:]) / atr_period

    def _compute_atr_stop(
        self,
        company_id: int,
        entry_price: Optional[float],
        session: Session,
        atr: Optional[float] = None,
    ) -> Optional[float]:
        """ATR-based stop-loss, clamped between min and max percentage.

        Uses ``ATR_MULTIPLIER × ATR(ATR_PERIOD)`` as the stop distance below
        the entry price. The percentage distance is clamped to
        ``[MIN_STOP_PCT, MAX_STOP_PCT]`` to avoid extreme values.

        Falls back to the fixed ``_STOP_LOSS_FACTOR`` when insufficient
        price data is available for ATR computation.

        Args:
            company_id: Database ID of the company.
            entry_price: Entry price (latest close).
            session: Active DB session.

        Returns:
            Stop-loss price, or None if entry_price is None.
        """
        if entry_price is None or entry_price <= 0:
            return None

        atr_mult = self._cfg.get("atr_multiplier", _ATR_MULTIPLIER)
        min_stop = self._cfg.get("min_stop_pct", _MIN_STOP_PCT)
        max_stop = self._cfg.get("max_stop_pct", _MAX_STOP_PCT)

        if atr is None:
            atr = self._atr_value(company_id, session)
        if atr is None:
            # Not enough data — fallback to fixed stop
            return round(entry_price * _STOP_LOSS_FACTOR, 2)

        # Stop distance as percentage of entry
        stop_distance_pct = (atr * atr_mult) / entry_price
        stop_distance_pct = max(min_stop, min(max_stop, stop_distance_pct))

        stop_price = entry_price * (1.0 - stop_distance_pct)

        logger.debug(
            "company_id=%d ATR=%.2f stop_dist=%.1f%% stop=%.2f",
            company_id, atr, stop_distance_pct * 100, stop_price,
        )

        return round(stop_price, 2)
