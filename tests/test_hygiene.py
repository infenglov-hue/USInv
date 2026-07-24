"""Phase 3.1 hygiene hard-gate tests (MODEL_SPEC §3).

Each gate is exercised with a known-positive and a known-negative case using
representative real 10-K/10-Q language, including an "alleviated going concern"
negative, and every firing verdict carries an evidence pointer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from usinv.hygiene import (
    HygieneAction,
    HygieneError,
    HygieneVerdict,
    ListingRiskEvidence,
    ShellEvidence,
    clear,
    evaluate_going_concern,
    evaluate_listing_risk,
    evaluate_shell,
    worst,
)

POINTER = "efts://filing/0000000000-25-000001"

# Representative real going-concern language.
_AFFIRMATIVE = (
    "As disclosed in Note 1, the Company has incurred recurring losses and "
    "negative cash flows from operations. These conditions raise substantial "
    "doubt about the Company's ability to continue as a going concern."
)
_NEGATED = (
    "Management evaluated the Company's ability to continue as a going concern "
    "and concluded that there is no substantial doubt about its ability to "
    "continue as a going concern for one year."
)
_ALLEVIATED = (
    "Conditions previously raised substantial doubt about the Company's ability "
    "to continue as a going concern; however, as a result of the financing "
    "completed in March, those conditions have been alleviated."
)
_BOILERPLATE = (
    "The accompanying financial statements have been prepared on a going concern "
    "basis, which contemplates the realization of assets in the normal course."
)


# --------------------------------------------------------------------------- #
# Verdict contract.
# --------------------------------------------------------------------------- #
def test_firing_verdict_requires_evidence_pointer():
    with pytest.raises(HygieneError):
        HygieneVerdict("shell", HygieneAction.EXCLUDE, "detail")


def test_clear_verdict_needs_no_pointer():
    verdict = clear("shell")
    assert verdict.action is HygieneAction.CLEAR
    assert verdict.blocks_selection is False


def test_worst_picks_most_restrictive():
    verdicts = [
        clear("a"),
        HygieneVerdict("b", HygieneAction.PENALIZE, "d", POINTER),
        HygieneVerdict("c", HygieneAction.EXCLUDE, "d", POINTER),
    ]
    assert worst(verdicts).action is HygieneAction.EXCLUDE
    assert worst([]) is None


# --------------------------------------------------------------------------- #
# Going-concern gate + negation/alleviation heuristic.
# --------------------------------------------------------------------------- #
def test_going_concern_affirmative_excludes():
    verdict = evaluate_going_concern(_AFFIRMATIVE, evidence_pointer=POINTER)
    assert verdict.action is HygieneAction.EXCLUDE
    assert verdict.evidence_pointer == POINTER


def test_going_concern_negated_is_clear():
    assert evaluate_going_concern(_NEGATED, evidence_pointer=POINTER).action is HygieneAction.CLEAR


def test_going_concern_alleviated_is_clear():
    assert (
        evaluate_going_concern(_ALLEVIATED, evidence_pointer=POINTER).action is HygieneAction.CLEAR
    )


def test_going_concern_basis_boilerplate_is_clear():
    assert (
        evaluate_going_concern(_BOILERPLATE, evidence_pointer=POINTER).action is HygieneAction.CLEAR
    )


def test_going_concern_affirmative_without_pointer_raises():
    with pytest.raises(HygieneError):
        evaluate_going_concern(_AFFIRMATIVE, evidence_pointer="")


# --------------------------------------------------------------------------- #
# Shell gate.
# --------------------------------------------------------------------------- #
def test_shell_sic_history_excludes():
    verdict = evaluate_shell(ShellEvidence(frozenset({3826, 6770}), None, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_shell_cover_flag_excludes():
    verdict = evaluate_shell(ShellEvidence(frozenset({3826}), True, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_shell_clean_is_clear():
    verdict = evaluate_shell(ShellEvidence(frozenset({3826}), False, POINTER))
    assert verdict.action is HygieneAction.CLEAR


# --------------------------------------------------------------------------- #
# Listing-compliance / delisting-clock gate.
# --------------------------------------------------------------------------- #
def test_listing_low_price_recent_reverse_split_excludes():
    verdict = evaluate_listing_risk(ListingRiskEvidence(Decimal("1.10"), True, True, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_listing_reverse_split_24m_penalizes():
    verdict = evaluate_listing_risk(ListingRiskEvidence(Decimal("8.00"), False, True, POINTER))
    assert verdict.action is HygieneAction.PENALIZE


def test_listing_low_price_without_reverse_split_is_clear():
    verdict = evaluate_listing_risk(ListingRiskEvidence(Decimal("1.10"), False, False, POINTER))
    assert verdict.action is HygieneAction.CLEAR
