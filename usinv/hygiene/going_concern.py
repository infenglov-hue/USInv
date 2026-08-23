"""Going-concern hard gate (MODEL_SPEC §3).

efts.sec.gov full-text flags ``"substantial doubt" "going concern"`` in the
latest 10-K/10-Q. Because ASU 2014-15 makes every issuer state whether such
doubt exists, the raw phrase match also fires on boilerplate and explicit
negations ("no substantial doubt", "alleviated"). A negation/alleviation
heuristic is therefore REQUIRED, not optional (CODEX_TASKS 3.1): only an
affirmative, non-negated substantial-doubt statement excludes.
"""

from __future__ import annotations

from usinv.hygiene.verdict import HygieneAction, HygieneVerdict, clear

_GOING_CONCERN = "going concern"
_SUBSTANTIAL_DOUBT = "substantial doubt"

# Markers that neutralise a nearby substantial-doubt phrase (doubt absent/resolved).
_NEGATIONS = (
    "no substantial doubt",
    "not raise substantial doubt",
    "not raise any substantial doubt",
    "no longer substantial doubt",
    "alleviated",
    "mitigated",
    "no material uncertainty",
    "no conditions or events",
)


def evaluate_going_concern(
    text: str,
    *,
    evidence_pointer: str,
    window: int = 300,
) -> HygieneVerdict:
    """Exclude only on an affirmative, non-negated going-concern doubt statement."""
    normalized = " ".join(text.lower().split())
    index = normalized.find(_GOING_CONCERN)
    while index != -1:
        span = normalized[max(0, index - window) : index + len(_GOING_CONCERN) + window]
        if _SUBSTANTIAL_DOUBT in span and not any(marker in span for marker in _NEGATIONS):
            return HygieneVerdict(
                gate="going_concern",
                action=HygieneAction.EXCLUDE,
                detail="affirmative substantial-doubt going-concern statement",
                evidence_pointer=evidence_pointer,
            )
        index = normalized.find(_GOING_CONCERN, index + 1)
    return clear("going_concern", "no affirmative going-concern doubt")
