"""Cross-sectional percentile ranking for factor sleeves (MODEL_SPEC §1).

Sleeve scores are cross-sectional: a security's rank is only meaningful relative
to the other securities scored at the same signal instant. Securities whose metric
is unavailable are excluded from the ranked population entirely — they never
receive a neutral mid-rank, because a fabricated median is a silent bias.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean


class RankingError(ValueError):
    """Raised when a ranking population is inconsistent."""


def percentile_ranks(values: Mapping[str, float | None]) -> dict[str, float | None]:
    """Rank *values* cross-sectionally into ``[0, 1]``; higher input ⇒ higher rank.

    Ties share the average of the ranks they span, so equal inputs always receive
    equal scores. Keys whose value is ``None`` map to ``None``.
    """
    observed = {key: value for key, value in values.items() if value is not None}
    ranks: dict[str, float | None] = dict.fromkeys(values)
    if not observed:
        return ranks
    count = len(observed)
    ordered = sorted(observed.items(), key=lambda item: item[1])
    index = 0
    while index < count:
        stop = index
        while stop + 1 < count and ordered[stop + 1][1] == ordered[index][1]:
            stop += 1
        # Average the 1-based positions this tie group spans, then map to (0, 1).
        average_position = (index + stop) / 2 + 1
        score = (average_position - 0.5) / count
        for key, _ in ordered[index : stop + 1]:
            ranks[key] = score
        index = stop + 1
    return ranks


def sleeve_score(metric_ranks: Sequence[float | None]) -> float | None:
    """Combine a security's available metric ranks into one sleeve score.

    Returns ``None`` when no metric in the sleeve could be computed: such a
    security is unscoreable for this sleeve and must not be given a default.
    """
    available = [rank for rank in metric_ranks if rank is not None]
    if not available:
        return None
    return fmean(available)


def rank_sleeve(
    metrics_by_security: Mapping[str, Mapping[str, float | None]],
) -> dict[str, float | None]:
    """Rank every metric cross-sectionally, then average each security's ranks."""
    if not metrics_by_security:
        return {}
    metric_names: list[str] = []
    for metrics in metrics_by_security.values():
        for name in metrics:
            if name not in metric_names:
                metric_names.append(name)
    ranked_by_metric = {
        name: percentile_ranks(
            {security_id: metrics.get(name) for security_id, metrics in metrics_by_security.items()}
        )
        for name in metric_names
    }
    return {
        security_id: sleeve_score([ranked_by_metric[name][security_id] for name in metric_names])
        for security_id in metrics_by_security
    }
