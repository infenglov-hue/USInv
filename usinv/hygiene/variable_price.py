"""Variable-price financing gate (MODEL_SPEC §3).

An outstanding convertible or equity line whose conversion/purchase price is
explicitly discounted to future market price ("toxic" / death-spiral financing)
excludes while the instrument is outstanding, when corroborated by a resale
registration or subsequent issuance. If the instrument is detected but its
maturity/status is unknown, quarantine rather than guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear


@dataclass(frozen=True, slots=True)
class VariablePriceEvidence:
    instrument_outstanding: bool
    corroborated: bool
    status_unknown: bool
    evidence_pointer: str


def evaluate_variable_price(evidence: VariablePriceEvidence) -> HygieneVerdict:
    """Exclude corroborated outstanding variable-price financing; quarantine if unclear."""
    if evidence.instrument_outstanding and evidence.corroborated:
        return HygieneVerdict(
            gate="variable_price",
            action=HygieneAction.EXCLUDE,
            detail="outstanding variable-price financing corroborated by resale/issuance",
            evidence_pointer=evidence.evidence_pointer,
        )
    if evidence.status_unknown:
        return HygieneVerdict(
            gate="variable_price",
            action=HygieneAction.QUARANTINE,
            detail="variable-price instrument with unknown maturity/status",
            evidence_pointer=evidence.evidence_pointer,
        )
    return clear("variable_price", "no variable-price financing markers")
