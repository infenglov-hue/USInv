"""Entry and hold-band rules for stateful portfolio transitions."""

from __future__ import annotations

from dataclasses import dataclass


class BandError(ValueError):
    """Raised when a rank or band is outside its declared domain."""


@dataclass(frozen=True, slots=True)
class BandPolicy:
    entry_top_fraction: float
    hold_top_fraction: float

    def __post_init__(self) -> None:
        if not 0 < self.entry_top_fraction <= self.hold_top_fraction <= 1:
            raise BandError("entry/hold fractions must satisfy 0 < entry <= hold <= 1")

    @staticmethod
    def _rank(percentile: float | None) -> float | None:
        if percentile is None:
            return None
        if not 0 <= percentile <= 1:
            raise BandError("percentile must be in [0, 1]")
        return percentile

    def permits_entry(self, percentile: float | None) -> bool:
        value = self._rank(percentile)
        return value is not None and value >= 1 - self.entry_top_fraction

    def permits_hold(self, percentile: float | None) -> bool:
        value = self._rank(percentile)
        return value is not None and value >= 1 - self.hold_top_fraction
