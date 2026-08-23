"""Piotroski-gated factor composite with size-bucket and FF12 peer controls."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from usinv.config.loader import FactorWeights
from usinv.scoring.quality import PiotroskiInputs, PiotroskiResult, piotroski_score
from usinv.scoring.ranking import percentile_ranks, rank_sleeve


class CompositeError(ValueError):
    """Raised when candidate or peer-group inputs violate the scoring contract."""


@dataclass(frozen=True, slots=True)
class FactorCandidate:
    """Raw factor metrics for one already universe/hygiene-eligible security."""

    security_id: str
    size_bucket: str
    ff12_group: str | None
    value_metrics: Mapping[str, float | None]
    quality_metrics: Mapping[str, float | None]
    momentum_metrics: Mapping[str, float | None]
    piotroski_inputs: PiotroskiInputs

    def __post_init__(self) -> None:
        if not self.security_id or not self.size_bucket:
            raise CompositeError("security_id and size_bucket must be non-empty")


@dataclass(frozen=True, slots=True)
class CompositeScore:
    """Every decision needed to reproduce one candidate's composite result."""

    security_id: str
    peer_group: str
    piotroski: PiotroskiResult
    eligible: bool
    exclusion_reason: str | None
    value_score: float | None
    quality_score: float | None
    momentum_score: float | None
    composite: float | None
    bucket_percentile: float | None


def _validate_weights(weights: FactorWeights) -> None:
    values = (weights.value, weights.quality, weights.momentum)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise CompositeError("factor weights must be finite and non-negative")
    if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
        raise CompositeError("factor weights must sum to one")


def _peer_key(candidate: FactorCandidate, sector_relative: bool) -> tuple[str, ...] | None:
    if not sector_relative:
        return (candidate.size_bucket,)
    if not candidate.ff12_group:
        return None
    return (candidate.size_bucket, candidate.ff12_group)


def _peer_label(key: tuple[str, ...] | None) -> str:
    return "/".join(key) if key else "unavailable"


def score_composite(
    candidates: Sequence[FactorCandidate],
    *,
    weights: FactorWeights,
    piotroski_veto_max: int,
    sector_relative: bool,
) -> dict[str, CompositeScore]:
    """Rank sleeves inside declared peers, apply the junk veto, and combine them.

    Core and large-cap candidates are always ranked independently. With
    ``sector_relative=True`` the peer is further narrowed to the candidate's FF12
    group. Piotroski failures and incomplete F-scores are removed *before*
    percentile fitting, so vetoed junk cannot distort eligible securities' ranks.
    Missing sleeve scores are not reweighted; the candidate is unscoreable.
    """
    _validate_weights(weights)
    if not 0 <= piotroski_veto_max <= 9:
        raise CompositeError("Piotroski veto threshold must be between 0 and 9")

    ordered = sorted(candidates, key=lambda item: item.security_id)
    if len({candidate.security_id for candidate in ordered}) != len(ordered):
        raise CompositeError("candidate security_ids must be unique")

    reports = {item.security_id: piotroski_score(item.piotroski_inputs) for item in ordered}
    exclusions: dict[str, str] = {}
    peers: dict[tuple[str, ...], list[FactorCandidate]] = defaultdict(list)
    for candidate in ordered:
        report = reports[candidate.security_id]
        vetoed = report.vetoed(piotroski_veto_max)
        if vetoed is None:
            exclusions[candidate.security_id] = "piotroski_incomplete"
            continue
        if vetoed:
            exclusions[candidate.security_id] = "piotroski_veto"
            continue
        peer = _peer_key(candidate, sector_relative)
        if peer is None:
            exclusions[candidate.security_id] = "missing_ff12_group"
            continue
        peers[peer].append(candidate)

    sleeve_scores: dict[str, tuple[float | None, float | None, float | None]] = {}
    for peer_candidates in peers.values():
        value = rank_sleeve({item.security_id: item.value_metrics for item in peer_candidates})
        quality = rank_sleeve({item.security_id: item.quality_metrics for item in peer_candidates})
        momentum = rank_sleeve(
            {item.security_id: item.momentum_metrics for item in peer_candidates}
        )
        for candidate in peer_candidates:
            security_id = candidate.security_id
            scores = (value[security_id], quality[security_id], momentum[security_id])
            sleeve_scores[security_id] = scores
            if any(score is None for score in scores):
                exclusions[security_id] = "missing_factor_sleeve"

    composites: dict[str, float] = {}
    for security_id, (value, quality, momentum) in sleeve_scores.items():
        if security_id in exclusions:
            continue
        assert value is not None and quality is not None and momentum is not None
        composites[security_id] = (
            value * weights.value + quality * weights.quality + momentum * weights.momentum
        )

    bucket_percentiles: dict[str, float | None] = {}
    for bucket in sorted({candidate.size_bucket for candidate in ordered}):
        bucket_ids = [
            candidate.security_id
            for candidate in ordered
            if candidate.size_bucket == bucket and candidate.security_id in composites
        ]
        bucket_percentiles.update(
            percentile_ranks({security_id: composites[security_id] for security_id in bucket_ids})
        )

    results: dict[str, CompositeScore] = {}
    for candidate in ordered:
        security_id = candidate.security_id
        scores = sleeve_scores.get(security_id, (None, None, None))
        peer = _peer_key(candidate, sector_relative)
        reason = exclusions.get(security_id)
        results[security_id] = CompositeScore(
            security_id=security_id,
            peer_group=_peer_label(peer),
            piotroski=reports[security_id],
            eligible=reason is None,
            exclusion_reason=reason,
            value_score=scores[0],
            quality_score=scores[1],
            momentum_score=scores[2],
            composite=composites.get(security_id),
            bucket_percentile=bucket_percentiles.get(security_id),
        )
    return results
