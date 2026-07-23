from __future__ import annotations

from datetime import UTC, datetime

import pytest

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.filing_header import parse_filing_header_metadata, parse_filing_sic


def test_filing_header_sic_is_accession_specific_evidence() -> None:
    body = b"<SEC-HEADER>\nSTANDARD INDUSTRIAL CLASSIFICATION: WATER SUPPLY [4941]\n"

    assert parse_filing_sic(body) == 4941
    assert parse_filing_sic(b"<SEC-HEADER>\nNO SIC HERE\n") is None


def test_conflicting_filing_header_sics_fail_closed() -> None:
    body = (
        b"STANDARD INDUSTRIAL CLASSIFICATION: FIRST [1234]\n"
        b"STANDARD INDUSTRIAL CLASSIFICATION: SECOND [5678]\n"
    )

    with pytest.raises(EdgarPayloadError, match="conflicting"):
        parse_filing_sic(body)


def test_filing_header_metadata_keeps_the_acceptance_instant() -> None:
    body = (
        b"<ACCEPTANCE-DATETIME>20260717155959\n"
        b"STANDARD INDUSTRIAL CLASSIFICATION: WATER SUPPLY [4941]\n"
    )

    metadata = parse_filing_header_metadata(body)

    assert metadata is not None
    assert metadata.accepted == datetime(2026, 7, 17, 15, 59, 59, tzinfo=UTC)
    assert metadata.sic == 4941
