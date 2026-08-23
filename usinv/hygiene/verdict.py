"""Shared hygiene verdict contract (MODEL_SPEC §3).

Every red-flag gate returns a :class:`HygieneVerdict`. A gate that fires must
carry an evidence pointer — "a flag that doesn't gate is not a feature" and every
exclusion is logged with evidence (MODEL_SPEC §3, D011).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum


class HygieneError(ValueError):
    """Raised when a hygiene verdict is constructed inconsistently."""


class HygieneAction(IntEnum):
    """Selection dispositions, ordered least → most restrictive for combining."""

    CLEAR = 0
    PENALIZE = 1
    QUARANTINE = 2
    EXCLUDE = 3


@dataclass(frozen=True, slots=True)
class HygieneVerdict:
    """One gate's disposition for one security, with mandatory evidence when it fires."""

    gate: str
    action: HygieneAction
    detail: str
    evidence_pointer: str = ""

    def __post_init__(self) -> None:
        if not self.gate:
            raise HygieneError("hygiene verdict requires a gate name")
        if self.action is not HygieneAction.CLEAR and not self.evidence_pointer:
            raise HygieneError(f"gate {self.gate!r} fired without an evidence pointer")

    @property
    def blocks_selection(self) -> bool:
        """Whether this verdict removes the security from selection (exclude/quarantine)."""
        return self.action >= HygieneAction.QUARANTINE


def clear(gate: str, detail: str = "no markers") -> HygieneVerdict:
    """Return a passing verdict for *gate*."""
    return HygieneVerdict(gate=gate, action=HygieneAction.CLEAR, detail=detail)


def worst(verdicts: Iterable[HygieneVerdict]) -> HygieneVerdict | None:
    """Return the most restrictive verdict, or ``None`` if there are none."""
    ordered = sorted(verdicts, key=lambda verdict: verdict.action, reverse=True)
    return ordered[0] if ordered else None
