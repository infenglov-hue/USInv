"""Listing-compliance / delisting-clock gate (MODEL_SPEC §3).

Exchange rules differ, but the conservative operational heuristic is a single
policy: a contemporaneous raw close below $1.50 together with a reverse split in
the prior 12 months excludes; any reverse split in the prior 24 months
penalizes. The $1.50 threshold is our risk policy, not exchange law.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear

DEFAULT_LOW_PRICE_THRESHOLD = Decimal("1.50")


@dataclass(frozen=True, slots=True)
class ListingRiskEvidence:
    raw_close: Decimal
    reverse_split_within_12m: bool
    reverse_split_within_24m: bool
    evidence_pointer: str


def evaluate_listing_risk(
    evidence: ListingRiskEvidence,
    *,
    low_price_threshold: Decimal = DEFAULT_LOW_PRICE_THRESHOLD,
) -> HygieneVerdict:
    """Exclude sub-threshold names with a recent reverse split; penalize a 24m split."""
    if evidence.raw_close < low_price_threshold and evidence.reverse_split_within_12m:
        return HygieneVerdict(
            gate="listing_risk",
            action=HygieneAction.EXCLUDE,
            detail=(
                f"raw close {evidence.raw_close} < {low_price_threshold} with reverse split in 12m"
            ),
            evidence_pointer=evidence.evidence_pointer,
        )
    if evidence.reverse_split_within_24m:
        return HygieneVerdict(
            gate="listing_risk",
            action=HygieneAction.PENALIZE,
            detail="reverse split within 24m",
            evidence_pointer=evidence.evidence_pointer,
        )
    return clear("listing_risk", "no listing-compliance risk markers")
