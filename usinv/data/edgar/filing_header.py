"""Parse filing-time metadata from an archived SEC complete-submission header."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from usinv.data.edgar.client import EdgarPayloadError

_SIC = re.compile(
    rb"STANDARD\s+INDUSTRIAL\s+CLASSIFICATION\s*:[^\r\n]*?\[(\d{3,4})\]",
    re.IGNORECASE,
)
_ACCEPTED = re.compile(rb"<ACCEPTANCE-DATETIME>\s*(\d{14})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FilingHeaderMetadata:
    accepted: datetime
    sic: int


def parse_filing_sic(body: bytes) -> int | None:
    """Return one unambiguous filing-header SIC, or ``None`` when absent."""

    values = {int(value) for value in _SIC.findall(body)}
    if len(values) > 1:
        raise EdgarPayloadError("complete submission contains conflicting SIC headers")
    if not values:
        return None
    sic = values.pop()
    if not 100 <= sic <= 9999:
        raise EdgarPayloadError("complete submission SIC is outside the valid range")
    return sic


def parse_filing_header_metadata(body: bytes) -> FilingHeaderMetadata | None:
    """Return acceptance-time SIC metadata when both header fields are unambiguous."""

    accepted_values = set(_ACCEPTED.findall(body))
    if len(accepted_values) > 1:
        raise EdgarPayloadError("complete submission contains conflicting acceptance headers")
    sic = parse_filing_sic(body)
    if not accepted_values or sic is None:
        return None
    try:
        accepted = datetime.strptime(
            accepted_values.pop().decode("ascii"),
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=UTC)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EdgarPayloadError("complete submission acceptance header is invalid") from exc
    return FilingHeaderMetadata(accepted, sic)
