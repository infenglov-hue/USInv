"""Data-integrity gate (MODEL_SPEC §3).

These are data-quality problems, not fundamentals verdicts: a share-count jump
over 50% with no detected split, a D030 applicability-aware standardization
coverage failure, or fundamentals stale past expected+grace. Any of them
quarantines the security (not scored) rather than excluding it as a red flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear


@dataclass(frozen=True, slots=True)
class DataIntegrityEvidence:
    share_count_jump_without_split: bool
    coverage_failure: bool
    stale_fundamentals: bool
    evidence_pointer: str


def evaluate_data_integrity(evidence: DataIntegrityEvidence) -> HygieneVerdict:
    """Quarantine (do not score) on any unresolved data-integrity problem."""
    reasons: list[str] = []
    if evidence.share_count_jump_without_split:
        reasons.append("share-count jump >50% without a detected split")
    if evidence.coverage_failure:
        reasons.append("D030 standardization coverage failure")
    if evidence.stale_fundamentals:
        reasons.append("fundamentals stale past expected+grace")
    if reasons:
        return HygieneVerdict(
            gate="data_integrity",
            action=HygieneAction.QUARANTINE,
            detail="; ".join(reasons),
            evidence_pointer=evidence.evidence_pointer,
        )
    return clear("data_integrity", "no data-integrity problems")
