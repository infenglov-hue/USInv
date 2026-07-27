"""Strict FRED JSON parser with caller-supplied official publication instants."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Final

from usinv.data.macro.vintage import MacroDataError, VintagedObservation

HY_OAS_SERIES: Final = "BAMLH0A0HYM2"
SAHM_REALTIME_SERIES: Final = "SAHMREALTIME"
SAHM_REVISED_SERIES: Final = "SAHMCURRENT"
NFCI_SERIES: Final = "NFCI"
VIX_SERIES: Final = "VIXCLS"


def parse_fred_observations(
    payload: bytes,
    *,
    series_id: str,
    publication_instants: Mapping[date, datetime],
    value_multiplier: float = 1.0,
    source: str = "FRED",
) -> tuple[VintagedObservation, ...]:
    """Parse FRED/ALFRED observations without guessing availability times.

    FRED's observation payload exposes real-time *dates*, not a reliable
    intraday publication instant. The acquisition layer must join the official
    release calendar and provide an exact timezone-aware instant for each
    ``realtime_start`` date. Missing joins fail closed.
    """
    if series_id == SAHM_REVISED_SERIES:
        raise MacroDataError("SAHMCURRENT is forbidden; use SAHMREALTIME")
    try:
        document = json.loads(payload)
        rows = document["observations"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise MacroDataError("invalid FRED observations JSON") from exc
    if not isinstance(rows, list):
        raise MacroDataError("FRED observations must be a list")

    payload_hash = hashlib.sha256(payload).hexdigest()
    parsed: list[VintagedObservation] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise MacroDataError("FRED observation row must be an object")
        raw_value = row.get("value")
        if raw_value in {None, ".", ""}:
            continue
        try:
            observation_date = date.fromisoformat(row["date"])
            vintage_date = date.fromisoformat(row["realtime_start"])
            value = float(raw_value) * value_multiplier
        except (KeyError, TypeError, ValueError) as exc:
            raise MacroDataError(f"invalid FRED observation at row {index}") from exc
        available_from = publication_instants.get(vintage_date)
        if available_from is None:
            raise MacroDataError(f"missing official publication instant for vintage {vintage_date}")
        if available_from.tzinfo is None:
            raise MacroDataError("FRED publication instants must be timezone-aware")
        parsed.append(
            VintagedObservation(
                series_id=series_id,
                observation_date=observation_date,
                available_from=available_from,
                value=value,
                source=source,
                evidence_pointer=f"{source.lower()}:{series_id}:{payload_hash}:{index}",
                vintage_date=vintage_date,
            )
        )
    return tuple(parsed)
