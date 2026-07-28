"""Deterministic stateful candidate selection for Phase 4.1."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from usinv.portfolio.bands import BandPolicy


class SelectionError(ValueError):
    """Raised when a selection population is ambiguous or inconsistent."""


@dataclass(frozen=True, slots=True)
class Candidate:
    security_id: str
    ticker: str
    size_bucket: str
    ff12_group: str
    bucket_percentile: float
    composite: float
    dollar_volume_21d: Decimal
    eligible: bool = True

    def __post_init__(self) -> None:
        if not all((self.security_id, self.ticker, self.size_bucket, self.ff12_group)):
            raise SelectionError("candidate identity, bucket and FF12 group are required")
        if not 0 <= self.bucket_percentile <= 1:
            raise SelectionError("candidate percentile must be in [0, 1]")
        if not math.isfinite(self.composite):
            raise SelectionError("candidate composite must be finite")
        if not self.dollar_volume_21d.is_finite() or self.dollar_volume_21d < 0:
            raise SelectionError("candidate dollar volume must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class Holding:
    position_id: str
    security_id: str


@dataclass(frozen=True, slots=True)
class Rejection:
    security_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class SelectionProposal:
    selected_security_ids: tuple[str, ...]
    retained_security_ids: tuple[str, ...]
    entry_security_ids: tuple[str, ...]
    forced_exit_security_ids: tuple[str, ...]
    band_exit_security_ids: tuple[str, ...]
    rejections: tuple[Rejection, ...]
    sector_cap_count: int


def _sector_cap(holdings: int, fraction: float) -> int:
    if holdings <= 0 or not 0 < fraction <= 1:
        raise SelectionError("holdings and sector cap fraction are invalid")
    return max(
        1,
        int((Decimal(holdings) * Decimal(str(fraction))).quantize(0, rounding=ROUND_HALF_UP)),
    )


def _correlation_passes(
    security_id: str,
    selected: tuple[str, ...],
    correlations: Mapping[tuple[str, str], float],
    threshold: float,
) -> tuple[bool, str | None]:
    for other in selected:
        key = tuple(sorted((security_id, other)))
        value = correlations.get(key)
        if value is None:
            return False, "missing_correlation"
        if not math.isfinite(value):
            return False, "invalid_correlation"
        if value >= threshold:
            return False, "correlation_cap"
    return True, None


def select_portfolio(
    candidates: tuple[Candidate, ...],
    holdings: tuple[Holding, ...],
    *,
    holdings_target: int,
    large_cap_max_slots: int,
    sector_cap_fraction: float,
    band_policy: BandPolicy,
    forced_exit_security_ids: frozenset[str] = frozenset(),
    correlations: Mapping[tuple[str, str], float] | None = None,
    correlation_enabled: bool = False,
    correlation_threshold: float = 0.7,
) -> SelectionProposal:
    """Build the desired transition before funding and turnover enforcement."""
    by_id = {item.security_id: item for item in candidates}
    if len(by_id) != len(candidates):
        raise SelectionError("candidate security ids must be unique")
    if len({item.security_id for item in holdings}) != len(holdings):
        raise SelectionError("holding security ids must be unique")
    if holdings_target <= 0 or not 0 <= large_cap_max_slots <= holdings_target:
        raise SelectionError("portfolio size or large-cap slot limit is invalid")
    if correlation_enabled and not 0 < correlation_threshold <= 1:
        raise SelectionError("correlation threshold must be in (0, 1]")

    cap = _sector_cap(holdings_target, sector_cap_fraction)
    retained: list[str] = []
    band_exits: list[str] = []
    rejections: list[Rejection] = []
    for holding in sorted(holdings, key=lambda item: (item.position_id, item.security_id)):
        security_id = holding.security_id
        if security_id in forced_exit_security_ids:
            continue
        candidate = by_id.get(security_id)
        if (
            candidate is None
            or not candidate.eligible
            or not band_policy.permits_hold(
                None if candidate is None else candidate.bucket_percentile
            )
        ):
            band_exits.append(security_id)
            continue
        retained.append(security_id)

    selected = list(retained)
    sector_counts = Counter(by_id[item].ff12_group for item in retained)
    large_count = sum(by_id[item].size_bucket == "large" for item in retained)
    held_ids = {item.security_id for item in holdings}
    ordered = sorted(
        (item for item in candidates if item.security_id not in held_ids),
        key=lambda item: (
            -item.bucket_percentile,
            -item.composite,
            -item.dollar_volume_21d,
            item.ticker,
            item.security_id,
        ),
    )
    for candidate in ordered:
        if len(selected) >= holdings_target:
            break
        if not candidate.eligible:
            rejections.append(Rejection(candidate.security_id, "ineligible"))
            continue
        if not band_policy.permits_entry(candidate.bucket_percentile):
            rejections.append(Rejection(candidate.security_id, "entry_band"))
            continue
        if candidate.size_bucket == "large" and large_count >= large_cap_max_slots:
            rejections.append(Rejection(candidate.security_id, "large_cap_limit"))
            continue
        if sector_counts[candidate.ff12_group] >= cap:
            rejections.append(Rejection(candidate.security_id, "sector_cap"))
            continue
        if correlation_enabled:
            passed, reason = _correlation_passes(
                candidate.security_id,
                tuple(selected),
                correlations or {},
                correlation_threshold,
            )
            if not passed:
                rejections.append(Rejection(candidate.security_id, reason or "correlation"))
                continue
        selected.append(candidate.security_id)
        sector_counts[candidate.ff12_group] += 1
        large_count += candidate.size_bucket == "large"

    entries = tuple(item for item in selected if item not in held_ids)
    return SelectionProposal(
        selected_security_ids=tuple(selected),
        retained_security_ids=tuple(retained),
        entry_security_ids=entries,
        forced_exit_security_ids=tuple(
            sorted(item for item in forced_exit_security_ids if item in held_ids)
        ),
        band_exit_security_ids=tuple(band_exits),
        rejections=tuple(rejections),
        sector_cap_count=cap,
    )
