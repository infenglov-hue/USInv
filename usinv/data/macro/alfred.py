"""ALFRED vintage enforcement for revised macro series."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from usinv.data.macro.fred import NFCI_SERIES, SAHM_REALTIME_SERIES, parse_fred_observations
from usinv.data.macro.vintage import MacroDataError, VintagedObservation


def parse_alfred_vintages(
    payload: bytes,
    *,
    series_id: str,
    publication_instants: Mapping[date, datetime],
    value_multiplier: float = 1.0,
) -> tuple[VintagedObservation, ...]:
    """Parse a revised series from an ALFRED all-vintages response."""
    if series_id != NFCI_SERIES:
        raise MacroDataError("ALFRED adapter is registered for revised NFCI only")
    observations = parse_fred_observations(
        payload,
        series_id=series_id,
        publication_instants=publication_instants,
        value_multiplier=value_multiplier,
        source="ALFRED",
    )
    if observations and any(item.vintage_date is None for item in observations):
        raise MacroDataError("ALFRED observations must retain a vintage_date")
    return observations


def parse_sahm_realtime(
    payload: bytes,
    *,
    publication_instants: Mapping[date, datetime],
) -> tuple[VintagedObservation, ...]:
    """Parse the registered real-time Sahm series; revised SAHMCURRENT is impossible."""
    return parse_fred_observations(
        payload,
        series_id=SAHM_REALTIME_SERIES,
        publication_instants=publication_instants,
        source="FRED",
    )
