"""Concentrated portfolio selection matching the MobileInv / BIST Picker model.

Implements a 5-slot (or configurable N-slot) high-conviction portfolio selector with:
- Incumbent hold band and turnover protection buffer:
  An incumbent holding is retained unless:
  (a) It is forced out (red flag, delisting, going concern), OR
  (b) It drops out of the top candidate pool, OR
  (c) A new candidate scores more than ``incumbent_turnover_threshold`` (default 1.15, +15%)
      higher than the incumbent.
- Max sector concentration cap (e.g. max 2 out of 5 from one sector).
- Dynamic ATR stop-loss and DCF/score-implied target price calculation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from usinv.portfolio.selector import Candidate, Holding, Rejection, SelectionError


@dataclass(frozen=True, slots=True)
class ConcentratedConfig:
    """Configuration for concentrated portfolio selection."""

    target_slots: int = 5
    max_per_sector: int = 2
    top_candidates_pool_size: int = 20
    incumbent_turnover_threshold: float = 1.15  # Challenger must be 15% better to replace
    stop_atr_multiplier: float = 2.0
    min_stop_pct: float = 0.10
    max_stop_pct: float = 0.25
    fallback_stop_pct: float = 0.18
    target_upside_min: float = 0.10
    target_max_multiple: float = 2.50

    def __post_init__(self) -> None:
        if self.target_slots <= 0:
            raise SelectionError("target_slots must be positive")
        if self.max_per_sector <= 0 or self.max_per_sector > self.target_slots:
            raise SelectionError("max_per_sector must be between 1 and target_slots")
        if self.incumbent_turnover_threshold < 1.0:
            raise SelectionError("incumbent_turnover_threshold must be >= 1.0")


@dataclass(frozen=True, slots=True)
class ConcentratedPick:
    """One selected security in the concentrated portfolio."""

    security_id: str
    ticker: str
    sector: str
    composite_score: float
    rank: int
    is_incumbent: bool
    entry_reference_price: float
    target_price: float
    stop_price: float
    target_upside_pct: float
    stop_loss_pct: float
    reason: str


@dataclass(frozen=True, slots=True)
class ConcentratedSelectionResult:
    """Full result of concentrated portfolio selection."""

    picks: tuple[ConcentratedPick, ...]
    retained_security_ids: tuple[str, ...]
    entry_security_ids: tuple[str, ...]
    forced_exit_security_ids: tuple[str, ...]
    replaced_exit_security_ids: tuple[str, ...]
    rejections: tuple[Rejection, ...]


def calculate_stop_and_target(
    entry_price: float,
    composite_score: float,
    *,
    atr: float | None = None,
    dcf_margin_of_safety: float | None = None,
    config: ConcentratedConfig | None = None,
) -> tuple[float, float]:
    """Calculate (stop_price, target_price) given entry price and parameters.

    Target price:
    - If valid dcf_margin_of_safety (0 < margin < 0.85): entry_price / (1 - margin)
    - Fallback: score-implied upside based on composite score (10% to 50% upside)
    - Capped at entry_price * target_max_multiple

    Stop loss:
    - If ATR provided: entry_price - (atr * multiplier), clamped to [min_stop_pct, max_stop_pct]
    - Fallback: entry_price * (1 - fallback_stop_pct)
    """
    cfg = config or ConcentratedConfig()
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    # 1. Target price calculation
    if dcf_margin_of_safety is not None and 0.05 <= dcf_margin_of_safety <= 0.85:
        raw_target = entry_price / (1.0 - dcf_margin_of_safety)
    else:
        # Score-implied upside: 100-score stock -> 25% upside; normalized to [10%, 60%]
        score_upside = max(cfg.target_upside_min, min(0.60, composite_score * 0.25))
        raw_target = entry_price * (1.0 + score_upside)

    target_price = round(min(raw_target, entry_price * cfg.target_max_multiple), 2)

    # 2. Stop loss calculation
    if atr is not None and atr > 0:
        atr_dist = atr * cfg.stop_atr_multiplier
        min_dist = entry_price * cfg.min_stop_pct
        max_dist = entry_price * cfg.max_stop_pct
        clamped_dist = max(min_dist, min(max_dist, atr_dist))
        stop_price = round(entry_price - clamped_dist, 2)
    else:
        stop_price = round(entry_price * (1.0 - cfg.fallback_stop_pct), 2)

    return stop_price, target_price


def select_concentrated_portfolio(
    candidates: tuple[Candidate, ...],
    holdings: tuple[Holding, ...],
    prices: Mapping[str, float],
    *,
    config: ConcentratedConfig | None = None,
    forced_exit_security_ids: frozenset[str] = frozenset(),
    atr_values: Mapping[str, float] | None = None,
    dcf_margins: Mapping[str, float] | None = None,
) -> ConcentratedSelectionResult:
    """Select the top 5 high-conviction stocks with incumbent retention and sector caps."""
    cfg = config or ConcentratedConfig()
    by_id = {item.security_id: item for item in candidates}
    held_ids = {h.security_id for h in holdings}

    # 1. Validate inputs
    for sid in held_ids:
        if sid in forced_exit_security_ids:
            continue
        if sid not in by_id:
            # Holding not in candidate list or delisted -> force exit
            forced_exit_security_ids = forced_exit_security_ids | {sid}

    # 2. Filter eligible candidates and sort by rank (composite desc, adv desc, ticker)
    eligible_candidates = [c for c in candidates if c.eligible]
    eligible_candidates.sort(
        key=lambda c: (
            -c.composite,
            -c.bucket_percentile,
            -c.dollar_volume_21d,
            c.ticker,
        )
    )

    top_pool = eligible_candidates[: cfg.top_candidates_pool_size]
    top_pool_ids = {c.security_id for c in top_pool}

    # 3. Process incumbent retention
    retained_ids: list[str] = []
    replaced_exit_ids: list[str] = []
    rejections: list[Rejection] = []

    # Best challenger candidate who is not currently held
    challengers = [c for c in top_pool if c.security_id not in held_ids]
    best_challenger_score = challengers[0].composite if challengers else 0.0

    for h in sorted(holdings, key=lambda item: (item.position_id, item.security_id)):
        sid = h.security_id
        if sid in forced_exit_security_ids:
            continue

        cand = by_id.get(sid)
        if cand is None or not cand.eligible:
            replaced_exit_ids.append(sid)
            rejections.append(Rejection(sid, "ineligible_or_missing"))
            continue

        if sid not in top_pool_ids:
            # Dropped out of the top pool entirely
            replaced_exit_ids.append(sid)
            rejections.append(Rejection(sid, "dropped_from_top_pool"))
            continue

        # Check turnover threshold: does a challenger beat incumbent by > threshold?
        if best_challenger_score > cand.composite * cfg.incumbent_turnover_threshold:
            # Significant challenger exists -> replace incumbent
            replaced_exit_ids.append(sid)
            rejections.append(Rejection(sid, "turnover_challenger_superior"))
            continue

        retained_ids.append(sid)

    # 4. Fill open slots up to target_slots respecting sector caps
    selected_ids = list(retained_ids)
    sector_counts = Counter(by_id[sid].ff12_group for sid in retained_ids)
    effective_sector_cap = min(cfg.max_per_sector, cfg.target_slots)

    for cand in eligible_candidates:
        if len(selected_ids) >= cfg.target_slots:
            break
        if cand.security_id in selected_ids:
            continue
        if cand.security_id in forced_exit_security_ids:
            rejections.append(Rejection(cand.security_id, "forced_exit"))
            continue
        if sector_counts[cand.ff12_group] >= effective_sector_cap:
            rejections.append(Rejection(cand.security_id, "sector_cap_exceeded"))
            continue

        selected_ids.append(cand.security_id)
        sector_counts[cand.ff12_group] += 1

    # 5. Build final ConcentratedPick objects
    atr_map = atr_values or {}
    dcf_map = dcf_margins or {}
    picks: list[ConcentratedPick] = []

    for rank_idx, sid in enumerate(selected_ids, start=1):
        cand = by_id[sid]
        is_inc = sid in held_ids
        entry_px = prices.get(sid, 100.0)
        atr_val = atr_map.get(sid)
        dcf_val = dcf_map.get(sid)

        stop_px, target_px = calculate_stop_and_target(
            entry_px,
            cand.composite,
            atr=atr_val,
            dcf_margin_of_safety=dcf_val,
            config=cfg,
        )

        upside_pct = round(((target_px - entry_px) / entry_px) * 100.0, 1)
        downside_pct = round(((stop_px - entry_px) / entry_px) * 100.0, 1)
        reason = "incumbent_retained" if is_inc else "top_factor_challenger"

        picks.append(
            ConcentratedPick(
                security_id=sid,
                ticker=cand.ticker,
                sector=cand.ff12_group,
                composite_score=round(cand.composite, 4),
                rank=rank_idx,
                is_incumbent=is_inc,
                entry_reference_price=entry_px,
                target_price=target_px,
                stop_price=stop_px,
                target_upside_pct=upside_pct,
                stop_loss_pct=downside_pct,
                reason=reason,
            )
        )

    entries = tuple(sid for sid in selected_ids if sid not in held_ids)
    forced_exits = tuple(sorted(sid for sid in forced_exit_security_ids if sid in held_ids))

    return ConcentratedSelectionResult(
        picks=tuple(picks),
        retained_security_ids=tuple(retained_ids),
        entry_security_ids=entries,
        forced_exit_security_ids=forced_exits,
        replaced_exit_security_ids=tuple(replaced_exit_ids),
        rejections=tuple(rejections),
    )
