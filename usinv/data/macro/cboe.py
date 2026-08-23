"""Cboe VIX historical CSV parser with explicit close-availability instants."""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping
from datetime import date, datetime

from usinv.data.macro.fred import VIX_SERIES
from usinv.data.macro.vintage import MacroDataError, VintagedObservation


def _parse_date(value: str) -> date:
    for parser in (date.fromisoformat, lambda item: datetime.strptime(item, "%m/%d/%Y").date()):
        try:
            return parser(value.strip())
        except ValueError:
            continue
    raise MacroDataError(f"invalid Cboe VIX date {value!r}")


def parse_cboe_vix_csv(
    payload: bytes,
    *,
    publication_instants: Mapping[date, datetime],
) -> tuple[VintagedObservation, ...]:
    """Parse official VIX closes; never infer the session-close instant."""
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MacroDataError("Cboe VIX CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise MacroDataError("Cboe VIX CSV has no header")
    normalized = {name.strip().upper(): name for name in reader.fieldnames}
    if not {"DATE", "CLOSE"} <= set(normalized):
        raise MacroDataError("Cboe VIX CSV requires DATE and CLOSE columns")
    digest = hashlib.sha256(payload).hexdigest()
    observations: list[VintagedObservation] = []
    for index, row in enumerate(reader):
        try:
            day = _parse_date(row[normalized["DATE"]])
            value = float(row[normalized["CLOSE"]])
        except (KeyError, TypeError, ValueError) as exc:
            raise MacroDataError(f"invalid Cboe VIX row {index}") from exc
        available_from = publication_instants.get(day)
        if available_from is None or available_from.tzinfo is None:
            raise MacroDataError(f"missing timezone-aware VIX availability for {day}")
        observations.append(
            VintagedObservation(
                series_id=VIX_SERIES,
                observation_date=day,
                available_from=available_from,
                value=value,
                source="CBOE",
                evidence_pointer=f"cboe:{VIX_SERIES}:{digest}:{index}",
                vintage_date=day,
            )
        )
    return tuple(observations)
