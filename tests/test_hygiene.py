"""Phase 3.1 hygiene hard-gate tests (MODEL_SPEC §3).

Each gate is exercised with a known-positive and a known-negative case using
representative real 10-K/10-Q language, including an "alleviated going concern"
negative, and every firing verdict carries an evidence pointer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from usinv.hygiene import (
    ATMDilutionEvidence,
    DataIntegrityEvidence,
    EnforcementEvidence,
    GoingConcernEvidence,
    HygieneAction,
    HygieneError,
    HygieneVerdict,
    ListingRiskEvidence,
    ShellEvidence,
    VariablePriceEvidence,
    clear,
    evaluate_atm_dilution,
    evaluate_data_integrity,
    evaluate_enforcement,
    evaluate_going_concern,
    evaluate_listing_risk,
    evaluate_shell,
    evaluate_variable_price,
    screen_hygiene,
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


# --------------------------------------------------------------------------- #
# ATM dilution gate.
# --------------------------------------------------------------------------- #
def test_atm_corroborated_issuance_excludes():
    verdict = evaluate_atm_dilution(ATMDilutionEvidence(True, True, True, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_atm_capacity_without_issuance_penalizes():
    verdict = evaluate_atm_dilution(ATMDilutionEvidence(True, True, False, POINTER))
    assert verdict.action is HygieneAction.PENALIZE


def test_atm_no_facility_is_clear():
    verdict = evaluate_atm_dilution(ATMDilutionEvidence(True, False, False, POINTER))
    assert verdict.action is HygieneAction.CLEAR


# --------------------------------------------------------------------------- #
# Variable-price financing gate.
# --------------------------------------------------------------------------- #
def test_variable_price_corroborated_outstanding_excludes():
    verdict = evaluate_variable_price(VariablePriceEvidence(True, True, False, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_variable_price_unknown_status_quarantines():
    verdict = evaluate_variable_price(VariablePriceEvidence(True, False, True, POINTER))
    assert verdict.action is HygieneAction.QUARANTINE


def test_variable_price_clean_is_clear():
    verdict = evaluate_variable_price(VariablePriceEvidence(False, False, False, POINTER))
    assert verdict.action is HygieneAction.CLEAR


# --------------------------------------------------------------------------- #
# Enforcement / suspension gate.
# --------------------------------------------------------------------------- #
def test_enforcement_active_suspension_excludes():
    verdict = evaluate_enforcement(EnforcementEvidence(True, 0, POINTER))
    assert verdict.action is HygieneAction.EXCLUDE


def test_enforcement_repeated_name_changes_penalize():
    verdict = evaluate_enforcement(EnforcementEvidence(False, 2, POINTER))
    assert verdict.action is HygieneAction.PENALIZE


def test_enforcement_single_name_change_is_clear():
    verdict = evaluate_enforcement(EnforcementEvidence(False, 1, POINTER))
    assert verdict.action is HygieneAction.CLEAR


# --------------------------------------------------------------------------- #
# Data-integrity gate.
# --------------------------------------------------------------------------- #
def test_data_integrity_share_jump_quarantines():
    verdict = evaluate_data_integrity(DataIntegrityEvidence(True, False, False, POINTER))
    assert verdict.action is HygieneAction.QUARANTINE


def test_data_integrity_clean_is_clear():
    verdict = evaluate_data_integrity(DataIntegrityEvidence(False, False, False, POINTER))
    assert verdict.action is HygieneAction.CLEAR


# --------------------------------------------------------------------------- #
# Aggregate screen.
# --------------------------------------------------------------------------- #
def _all_mandatory(going_concern_text: str = _NEGATED, **overrides):
    """Supply every mandatory gate so the completeness guard stays silent."""
    evidence = {
        "going_concern": GoingConcernEvidence(going_concern_text, POINTER),
        "shell": ShellEvidence(frozenset({3826}), False, POINTER),
        "listing": ListingRiskEvidence(Decimal("8.00"), False, False, POINTER),
        "data_integrity": DataIntegrityEvidence(False, False, False, POINTER),
    }
    evidence.update(overrides)
    return evidence


def test_screen_combines_to_most_restrictive():
    result = screen_hygiene(
        "SEC-1",
        **_all_mandatory(
            _AFFIRMATIVE,
            listing=ListingRiskEvidence(Decimal("8.00"), False, True, POINTER),
        ),
    )
    # going-concern EXCLUDE dominates the listing PENALIZE.
    assert result.action is HygieneAction.EXCLUDE
    assert result.blocks_selection is True
    assert len(result.firing) == 2  # going-concern + listing fired; shell/integrity clear


def test_screen_all_clear_does_not_block():
    result = screen_hygiene("SEC-2", **_all_mandatory())
    assert result.action is HygieneAction.CLEAR
    assert result.blocks_selection is False
    assert result.firing == ()


def test_screen_with_no_evidence_quarantines():
    # Missing evidence is not clean evidence: the screen must fail closed.
    result = screen_hygiene("SEC-3")
    assert result.action is HygieneAction.QUARANTINE
    assert result.blocks_selection is True
    assert "going_concern" in result.overall.detail


def test_screen_partial_evidence_quarantines():
    result = screen_hygiene("SEC-4", shell=ShellEvidence(frozenset({3826}), False, POINTER))
    assert result.action is HygieneAction.QUARANTINE
    assert "data_integrity" in result.overall.detail


def test_screen_opt_out_skips_completeness_guard():
    result = screen_hygiene(
        "SEC-5",
        shell=ShellEvidence(frozenset({3826}), False, POINTER),
        require_evidence=False,
    )
    assert result.action is HygieneAction.CLEAR
    assert result.blocks_selection is False


def test_screen_exclusion_still_wins_over_missing_evidence():
    # A hard exclusion must not be masked by the completeness quarantine.
    result = screen_hygiene("SEC-6", shell=ShellEvidence(frozenset({6770}), None, POINTER))
    assert result.action is HygieneAction.EXCLUDE
