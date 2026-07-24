"""Shell / blank-check hard gate (MODEL_SPEC §3).

Excludes on SIC 6770 in the filing history (SEC reclassifies after de-SPAC, so
"now or ever" is required) or ``dei:EntityShellCompany = true`` on the latest
10-K/10-Q cover page.
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear

SHELL_SIC = 6770


@dataclass(frozen=True, slots=True)
class ShellEvidence:
    sic_codes_ever: frozenset[int]
    entity_shell_flag: bool | None
    evidence_pointer: str


def evaluate_shell(evidence: ShellEvidence) -> HygieneVerdict:
    """Exclude a shell / blank-check issuer on SIC-6770 history or the cover flag."""
    reasons: list[str] = []
    if SHELL_SIC in evidence.sic_codes_ever:
        reasons.append("SIC 6770 present in filing history")
    if evidence.entity_shell_flag is True:
        reasons.append("dei:EntityShellCompany=true on latest cover")
    if reasons:
        return HygieneVerdict(
            gate="shell",
            action=HygieneAction.EXCLUDE,
            detail="; ".join(reasons),
            evidence_pointer=evidence.evidence_pointer,
        )
    return clear("shell", "no shell markers")
