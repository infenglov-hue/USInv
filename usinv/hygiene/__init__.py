"""Selection-blocking evidence gates (MODEL_SPEC §3)."""

from usinv.hygiene.dilution import ATMDilutionEvidence, evaluate_atm_dilution
from usinv.hygiene.enforcement import (
    NAME_CHANGE_PENALTY_THRESHOLD,
    EnforcementEvidence,
    evaluate_enforcement,
)
from usinv.hygiene.going_concern import evaluate_going_concern
from usinv.hygiene.integrity import DataIntegrityEvidence, evaluate_data_integrity
from usinv.hygiene.listing import (
    DEFAULT_LOW_PRICE_THRESHOLD,
    ListingRiskEvidence,
    evaluate_listing_risk,
)
from usinv.hygiene.screen import (
    GoingConcernEvidence,
    HygieneScreenResult,
    screen_hygiene,
)
from usinv.hygiene.shell import SHELL_SIC, ShellEvidence, evaluate_shell
from usinv.hygiene.variable_price import VariablePriceEvidence, evaluate_variable_price
from usinv.hygiene.verdict import (
    HygieneAction,
    HygieneError,
    HygieneVerdict,
    clear,
    worst,
)

__all__ = [
    "DEFAULT_LOW_PRICE_THRESHOLD",
    "NAME_CHANGE_PENALTY_THRESHOLD",
    "SHELL_SIC",
    "ATMDilutionEvidence",
    "DataIntegrityEvidence",
    "EnforcementEvidence",
    "GoingConcernEvidence",
    "HygieneAction",
    "HygieneError",
    "HygieneScreenResult",
    "HygieneVerdict",
    "ListingRiskEvidence",
    "ShellEvidence",
    "VariablePriceEvidence",
    "clear",
    "evaluate_atm_dilution",
    "evaluate_data_integrity",
    "evaluate_enforcement",
    "evaluate_going_concern",
    "evaluate_listing_risk",
    "evaluate_shell",
    "evaluate_variable_price",
    "screen_hygiene",
    "worst",
]
