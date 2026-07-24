"""Aggregate hygiene screen: run every MODEL_SPEC §3 gate and combine verdicts.

The overall disposition is the most restrictive gate verdict. Only the gates for
which evidence is supplied are evaluated; a security with no evidence is treated
as clear (the caller is responsible for supplying the required inputs).
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.dilution import ATMDilutionEvidence, evaluate_atm_dilution
from usinv.hygiene.enforcement import EnforcementEvidence, evaluate_enforcement
from usinv.hygiene.going_concern import evaluate_going_concern
from usinv.hygiene.integrity import DataIntegrityEvidence, evaluate_data_integrity
from usinv.hygiene.listing import ListingRiskEvidence, evaluate_listing_risk
from usinv.hygiene.shell import ShellEvidence, evaluate_shell
from usinv.hygiene.variable_price import VariablePriceEvidence, evaluate_variable_price
from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear, worst


@dataclass(frozen=True, slots=True)
class GoingConcernEvidence:
    text: str
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class HygieneScreenResult:
    security_id: str
    verdicts: tuple[HygieneVerdict, ...]

    @property
    def overall(self) -> HygieneVerdict:
        blocking = worst(self.verdicts)
        return blocking if blocking is not None else clear("hygiene", "no gates evaluated")

    @property
    def action(self) -> HygieneAction:
        return self.overall.action

    @property
    def blocks_selection(self) -> bool:
        return self.overall.blocks_selection

    @property
    def firing(self) -> tuple[HygieneVerdict, ...]:
        return tuple(v for v in self.verdicts if v.action is not HygieneAction.CLEAR)


def screen_hygiene(
    security_id: str,
    *,
    going_concern: GoingConcernEvidence | None = None,
    shell: ShellEvidence | None = None,
    listing: ListingRiskEvidence | None = None,
    atm_dilution: ATMDilutionEvidence | None = None,
    variable_price: VariablePriceEvidence | None = None,
    enforcement: EnforcementEvidence | None = None,
    data_integrity: DataIntegrityEvidence | None = None,
) -> HygieneScreenResult:
    """Evaluate every supplied gate and return the combined hygiene disposition."""
    verdicts: list[HygieneVerdict] = []
    if going_concern is not None:
        verdicts.append(
            evaluate_going_concern(
                going_concern.text, evidence_pointer=going_concern.evidence_pointer
            )
        )
    if shell is not None:
        verdicts.append(evaluate_shell(shell))
    if listing is not None:
        verdicts.append(evaluate_listing_risk(listing))
    if atm_dilution is not None:
        verdicts.append(evaluate_atm_dilution(atm_dilution))
    if variable_price is not None:
        verdicts.append(evaluate_variable_price(variable_price))
    if enforcement is not None:
        verdicts.append(evaluate_enforcement(enforcement))
    if data_integrity is not None:
        verdicts.append(evaluate_data_integrity(data_integrity))
    return HygieneScreenResult(security_id, tuple(verdicts))
