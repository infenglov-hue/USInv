from __future__ import annotations

from datetime import UTC, datetime

from usinv.data.edgar.filing_header import parse_filing_header_metadata, parse_filing_sic


def test_filing_header_sic_is_accession_specific_evidence() -> None:
    body = b"<SEC-HEADER>\nSTANDARD INDUSTRIAL CLASSIFICATION: WATER SUPPLY [4941]\n"

    assert parse_filing_sic(body) == 4941
    assert parse_filing_sic(b"<SEC-HEADER>\nNO SIC HERE\n") is None


def test_multi_registrant_header_uses_the_filers_own_first_sic() -> None:
    """Co-registrant blocks repeat the SIC line with different codes; the first
    match is the filer's own COMPANY DATA section. Later conflicts must not
    abort a 396-CIK snapshot acquisition (observed in the gate chain)."""
    body = (
        b"STANDARD INDUSTRIAL CLASSIFICATION: FIRST [1234]\n"
        b"STANDARD INDUSTRIAL CLASSIFICATION: SECOND [5678]\n"
    )

    assert parse_filing_sic(body) == 1234


def test_filing_header_metadata_keeps_the_acceptance_instant() -> None:
    body = (
        b"<ACCEPTANCE-DATETIME>20260717155959\n"
        b"STANDARD INDUSTRIAL CLASSIFICATION: WATER SUPPLY [4941]\n"
    )

    metadata = parse_filing_header_metadata(body)

    assert metadata is not None
    assert metadata.accepted == datetime(2026, 7, 17, 15, 59, 59, tzinfo=UTC)
    assert metadata.sic == 4941


def test_814_industry_fallback_is_explicit_and_bound_to_the_target_cik() -> None:
    body = (
        b"<ACCEPTANCE-DATETIME>20260717155959\n"
        b"CENTRAL INDEX KEY: 0001287750\n"
        b"SEC FILE NUMBER: 814-00663\n"
    )

    assert parse_filing_header_metadata(body) is None
    assert (
        parse_filing_header_metadata(
            body,
            cik=999,
            allow_814_industry_fallback=True,
        )
        is None
    )
    metadata = parse_filing_header_metadata(
        body,
        cik=1287750,
        allow_814_industry_fallback=True,
    )

    assert metadata is not None
    assert metadata.sic == 6726
    assert metadata.source_kind == "sec_file_number_814"
