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
_CIK = re.compile(rb"CENTRAL\s+INDEX\s+KEY\s*:\s*0*(\d+)", re.IGNORECASE)
_SEC_FILE_NUMBER = re.compile(rb"SEC\s+FILE\s+NUMBER\s*:\s*([0-9]+-[0-9-]+)", re.IGNORECASE)
_INVESTMENT_OFFICES_NEC_SIC = 6726


@dataclass(frozen=True, slots=True)
class FilingHeaderMetadata:
    accepted: datetime
    sic: int
    source_kind: str = "header_sic"


def parse_filing_sic(body: bytes) -> int | None:
    """Return one unambiguous filing-header SIC, or ``None`` when absent."""

    matches = [int(value) for value in _SIC.findall(body)]
    if not matches:
        return None
    # Multi-registrant submissions repeat the SIC line once per co-registrant and
    # occasionally carry stale legacy lines; the FIRST match is the filer's own
    # COMPANY DATA section, which always precedes any other registrant block.
    # Conflicting later values must not fail the whole snapshot acquisition.
    sic = matches[0]
    if not 100 <= sic <= 9999:
        raise EdgarPayloadError("complete submission SIC is outside the valid range")
    return sic


def parse_filing_header_metadata(
    body: bytes,
    *,
    cik: int | None = None,
    allow_814_industry_fallback: bool = False,
) -> FilingHeaderMetadata | None:
    """Return acceptance-time SIC metadata when both header fields are unambiguous."""

    accepted_values = set(_ACCEPTED.findall(body))
    if len(accepted_values) > 1:
        raise EdgarPayloadError("complete submission contains conflicting acceptance headers")
    sic = parse_filing_sic(body)
    source_kind = "header_sic"
    if sic is None and allow_814_industry_fallback:
        header_ciks = {int(value) for value in _CIK.findall(body)}
        file_numbers = {value.decode("ascii") for value in _SEC_FILE_NUMBER.findall(body)}
        if (
            cik is not None
            and cik in header_ciks
            and any(value.startswith("814-") for value in file_numbers)
        ):
            sic = _INVESTMENT_OFFICES_NEC_SIC
            source_kind = "sec_file_number_814"
    if not accepted_values or sic is None:
        return None
    try:
        accepted = datetime.strptime(
            accepted_values.pop().decode("ascii"),
            "%Y%m%d%H%M%S",
        ).replace(tzinfo=UTC)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EdgarPayloadError("complete submission acceptance header is invalid") from exc
    return FilingHeaderMetadata(accepted, sic, source_kind)
