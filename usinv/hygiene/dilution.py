"""Active ATM dilution gate (MODEL_SPEC §3).

An open S-3 shelf plus an ATM sales agreement is dilution *capacity*, not proof
of selling. A 424B5 can establish or amend capacity but never proves completed
selling on its own. Exclude ONLY when active issuance is corroborated (a later
8-K/10-Q equity roll-forward, a share-count increase, or repeated supplements);
otherwise penalize the standing capacity.
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear


@dataclass(frozen=True, slots=True)
class ATMDilutionEvidence:
    has_s3_shelf: bool
    has_atm_sales_agreement: bool
    issuance_corroborated: bool
    evidence_pointer: str


def evaluate_atm_dilution(evidence: ATMDilutionEvidence) -> HygieneVerdict:
    """Exclude on corroborated active ATM issuance; penalize standing capacity."""
    if not (evidence.has_s3_shelf and evidence.has_atm_sales_agreement):
        return clear("atm_dilution", "no active ATM facility")
    if evidence.issuance_corroborated:
        return HygieneVerdict(
            gate="atm_dilution",
            action=HygieneAction.EXCLUDE,
            detail="S-3 shelf + ATM agreement with corroborated active issuance",
            evidence_pointer=evidence.evidence_pointer,
        )
    return HygieneVerdict(
        gate="atm_dilution",
        action=HygieneAction.PENALIZE,
        detail="active ATM capacity without corroborated issuance",
        evidence_pointer=evidence.evidence_pointer,
    )
