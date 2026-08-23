"""Vintage-aware macroeconomic data adapters."""

from usinv.data.macro.alfred import parse_alfred_vintages, parse_sahm_realtime
from usinv.data.macro.archive import MacroArchiveRecord, MacroRawArchive, redact_request_url
from usinv.data.macro.cboe import parse_cboe_vix_csv
from usinv.data.macro.fred import (
    HY_OAS_SERIES,
    NFCI_SERIES,
    SAHM_REALTIME_SERIES,
    SAHM_REVISED_SERIES,
    VIX_SERIES,
    parse_fred_observations,
)
from usinv.data.macro.proxies import CreditProxySignal, hyg_lqd_credit_stress
from usinv.data.macro.vintage import (
    MacroDataError,
    VintagedObservation,
    latest_observation_as_of,
    observations_as_of,
)

__all__ = [
    "HY_OAS_SERIES",
    "NFCI_SERIES",
    "SAHM_REALTIME_SERIES",
    "SAHM_REVISED_SERIES",
    "VIX_SERIES",
    "CreditProxySignal",
    "MacroArchiveRecord",
    "MacroDataError",
    "MacroRawArchive",
    "VintagedObservation",
    "hyg_lqd_credit_stress",
    "latest_observation_as_of",
    "observations_as_of",
    "parse_alfred_vintages",
    "parse_cboe_vix_csv",
    "parse_fred_observations",
    "parse_sahm_realtime",
    "redact_request_url",
]
