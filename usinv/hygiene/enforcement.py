"""Enforcement / pivot-marker gate (MODEL_SPEC §3).

An active SEC trading suspension excludes. Two or more filing-time legal-name
changes within 36 months is a pivot/pump marker and penalizes. Unexplained
price/volume spikes are diagnostic only in v1 (no reliable promotion-data
source) and never gate selection here.
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear

NAME_CHANGE_PENALTY_THRESHOLD = 2


@dataclass(frozen=True, slots=True)
class EnforcementEvidence:
    active_trading_suspension: bool
    legal_name_changes_36m: int
    evidence_pointer: str


def evaluate_enforcement(evidence: EnforcementEvidence) -> HygieneVerdict:
    """Exclude on active suspension; penalize repeated legal-name churn."""
    if evidence.active_trading_suspension:
        return HygieneVerdict(
            gate="enforcement",
            action=HygieneAction.EXCLUDE,
            detail="active SEC trading suspension",
            evidence_pointer=evidence.evidence_pointer,
        )
    if evidence.legal_name_changes_36m >= NAME_CHANGE_PENALTY_THRESHOLD:
        return HygieneVerdict(
            gate="enforcement",
            action=HygieneAction.PENALIZE,
            detail=f"{evidence.legal_name_changes_36m} legal-name changes within 36 months",
            evidence_pointer=evidence.evidence_pointer,
        )
    return clear("enforcement", "no enforcement/pivot markers")
