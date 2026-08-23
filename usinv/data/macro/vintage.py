"""Canonical point-in-time observations for macro and market regime series."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime


class MacroDataError(ValueError):
    """Raised when macro evidence is ambiguous, malformed, or not point-in-time."""


@dataclass(frozen=True, slots=True)
class VintagedObservation:
    """One observation value and the instant that exact vintage became usable."""

    series_id: str
    observation_date: date
    available_from: datetime
    value: float
    source: str
    evidence_pointer: str
    vintage_date: date | None = None

    def __post_init__(self) -> None:
        if not self.series_id or not self.source or not self.evidence_pointer:
            raise MacroDataError("series_id, source and evidence_pointer must be non-empty")
        if self.available_from.tzinfo is None:
            raise MacroDataError("available_from must be timezone-aware")
        if not math.isfinite(self.value):
            raise MacroDataError("macro values must be finite")


def observations_as_of(
    observations: Iterable[VintagedObservation],
    *,
    series_id: str,
    as_of: datetime,
) -> tuple[VintagedObservation, ...]:
    """Resolve the latest known vintage per observation date at ``as_of``.

    A future revision is invisible. Equal-availability conflicting values are
    rejected instead of being resolved by input order.
    """
    if as_of.tzinfo is None:
        raise MacroDataError("as_of must be timezone-aware")
    grouped: dict[date, list[VintagedObservation]] = defaultdict(list)
    for observation in observations:
        if observation.series_id != series_id:
            raise MacroDataError(
                f"expected series {series_id!r}, received {observation.series_id!r}"
            )
        if observation.available_from <= as_of:
            grouped[observation.observation_date].append(observation)

    resolved: list[VintagedObservation] = []
    for observation_date, candidates in grouped.items():
        latest_at = max(item.available_from for item in candidates)
        latest = [item for item in candidates if item.available_from == latest_at]
        if len({item.value for item in latest}) != 1:
            raise MacroDataError(
                f"conflicting {series_id} values for {observation_date} at {latest_at.isoformat()}"
            )
        resolved.append(min(latest, key=lambda item: item.evidence_pointer))
    return tuple(sorted(resolved, key=lambda item: item.observation_date))


def latest_observation_as_of(
    observations: Iterable[VintagedObservation],
    *,
    series_id: str,
    as_of: datetime,
) -> VintagedObservation | None:
    """Return the latest visible observation, or ``None`` when none is usable."""
    history = observations_as_of(observations, series_id=series_id, as_of=as_of)
    return history[-1] if history else None
