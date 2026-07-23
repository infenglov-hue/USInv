from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from usinv.data.edgar.name_discovery import (
    EdgarCompanyName,
    augment_discovery_plan_with_exact_name_evidence,
    augment_discovery_plan_with_exact_names,
    fsds_company_name_observations,
    match_discovered_listing_names,
    match_exact_company_name,
    match_unmapped_listing_names,
    match_unmapped_listing_stems,
    normalize_company_name,
    normalize_company_stem,
)
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan, FilingDiscoveryRow


def test_exact_name_discovery_ignores_only_presentation_differences() -> None:
    observations = (
        EdgarCompanyName(100, "Reed's, Inc.", "sec-name://source/1"),
        EdgarCompanyName(200, "Reeds LLC", "sec-name://source/2"),
    )

    match = match_exact_company_name("REEDS INC", observations)

    assert match.status == "unique"
    assert match.candidate_ciks == (100,)
    assert match.evidence_pointers == ("sec-name://source/1",)
    assert normalize_company_name("Réed's, Inc.") == "reedsinc"


def test_exact_name_discovery_never_collapses_legal_suffixes() -> None:
    match = match_exact_company_name(
        "Example Inc",
        (EdgarCompanyName(100, "Example LLC", "sec-name://source/1"),),
    )

    assert match.status == "unmatched"
    assert match.candidate_ciks == ()


def test_legal_stem_is_only_a_suffix_insensitive_discovery_key() -> None:
    assert normalize_company_stem("The Example Holdings, Inc. - Class A") == "example"
    assert normalize_company_stem("Example LLC") == "example"


def test_exact_name_discovery_preserves_ambiguity_and_all_provenance() -> None:
    observations = (
        EdgarCompanyName(200, "Shared Corp.", "sec-name://source/3"),
        EdgarCompanyName(100, "Shared Corp", "sec-name://source/2"),
        EdgarCompanyName(100, "SHARED CORP", "sec-name://source/1"),
    )

    match = match_exact_company_name("Shared Corp", observations)

    assert match.status == "ambiguous"
    assert match.candidate_ciks == (100, 200)
    assert match.evidence_pointers == (
        "sec-name://source/1",
        "sec-name://source/2",
        "sec-name://source/3",
    )


def test_blank_name_is_not_a_candidate_and_invalid_evidence_fails() -> None:
    assert match_exact_company_name("  ", ()).status == "blank"
    with pytest.raises(ValueError, match="incomplete"):
        EdgarCompanyName(0, "Issuer", "sec-name://source/1")


def test_only_unmapped_rows_receive_name_discovery_candidates() -> None:
    pointer = f"alpha-vantage://{'a' * 64}/2"
    listing = type(
        "Listing",
        (),
        {
            "as_of": date(2026, 7, 17),
            "snapshot_id": "b" * 64,
            "rows": (
                type(
                    "Row",
                    (),
                    {
                        "state": "active",
                        "source_sha256": "a" * 64,
                        "row_number": 2,
                        "name": "Issuer Inc",
                    },
                )(),
            ),
        },
    )()
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "b" * 64,
        "c" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ISSR",
                "NYSE",
                "NYSE",
                "Stock",
                pointer,
                "unmapped",
                (),
            ),
        ),
    )

    matches = match_unmapped_listing_names(
        listing,
        discovery,
        (EdgarCompanyName(123, "ISSUER, INC.", "sec-fsds://source/accession"),),
    )

    assert matches[pointer].status == "unique"
    assert matches[pointer].candidate_ciks == (123,)

    augmented = augment_discovery_plan_with_exact_names(discovery, matches)
    row = augmented.rows[0]
    assert row.status == "discovered"
    assert row.candidate_ciks == (123,)
    assert row.candidate_evidence_pointers == (
        "sec-fsds://source/accession",
    )
    assert augmented.version == "usinv-sec-filing-discovery-v5"


def test_association_only_candidate_receives_exact_pit_name_provenance() -> None:
    pointer = f"alpha-vantage://{'a' * 64}/2"
    listing = type(
        "Listing",
        (),
        {
            "as_of": date(2026, 7, 17),
            "snapshot_id": "b" * 64,
            "rows": (
                type(
                    "Row",
                    (),
                    {
                        "state": "active",
                        "source_sha256": "a" * 64,
                        "row_number": 2,
                        "name": "Issuer Inc",
                    },
                )(),
            ),
        },
    )()
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "b" * 64,
        "c" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "OLD",
                "NYSE",
                "NYSE",
                "Stock",
                pointer,
                "discovered",
                (123,),
            ),
        ),
    )

    matches = match_discovered_listing_names(
        listing,
        discovery,
        (EdgarCompanyName(123, "ISSUER, INC.", "sec-fsds://source/accession"),),
    )
    augmented = augment_discovery_plan_with_exact_name_evidence(discovery, matches)

    assert augmented.rows[0].candidate_ciks == (123,)
    assert augmented.rows[0].candidate_evidence_pointers == (
        "sec-fsds://source/accession",
    )


def test_name_corroboration_cannot_replace_or_ambiguously_support_candidate() -> None:
    pointers = tuple(f"alpha-vantage://{'a' * 64}/{row}" for row in (2, 3))
    listing = type(
        "Listing",
        (),
        {
            "as_of": date(2026, 7, 17),
            "snapshot_id": "b" * 64,
            "rows": tuple(
                type(
                    "Row",
                    (),
                    {
                        "state": "active",
                        "source_sha256": "a" * 64,
                        "row_number": row_number,
                        "name": name,
                    },
                )()
                for row_number, name in ((2, "Wrong Inc"), (3, "Shared Inc"))
            ),
        },
    )()
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "b" * 64,
        "c" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        tuple(
            FilingDiscoveryRow(
                ticker,
                "NYSE",
                "NYSE",
                "Stock",
                pointer,
                "discovered",
                (123,),
            )
            for ticker, pointer in zip(("OLD1", "OLD2"), pointers, strict=True)
        ),
    )

    matches = match_discovered_listing_names(
        listing,
        discovery,
        (
            EdgarCompanyName(999, "Wrong Inc", "sec-fsds://source/wrong"),
            EdgarCompanyName(123, "Shared Inc", "sec-fsds://source/shared-1"),
            EdgarCompanyName(999, "Shared Inc", "sec-fsds://source/shared-2"),
        ),
    )
    augmented = augment_discovery_plan_with_exact_name_evidence(discovery, matches)

    assert all(not row.candidate_evidence_pointers for row in augmented.rows)


def test_stem_discovery_adds_candidate_with_fsds_provenance() -> None:
    pointer = f"alpha-vantage://{'a' * 64}/2"
    listing = type(
        "Listing",
        (),
        {
            "as_of": date(2026, 7, 17),
            "snapshot_id": "b" * 64,
            "rows": (
                type(
                    "Row",
                    (),
                    {
                        "state": "active",
                        "source_sha256": "a" * 64,
                        "row_number": 2,
                        "name": "Issuer Holdings Inc - Class A",
                    },
                )(),
            ),
        },
    )()
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "b" * 64,
        "c" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ISSR",
                "NYSE",
                "NYSE",
                "Stock",
                pointer,
                "unmapped",
                (),
            ),
        ),
    )

    matches = match_unmapped_listing_stems(
        listing,
        discovery,
        (EdgarCompanyName(123, "Issuer Corp", "sec-fsds://source/accession"),),
    )
    augmented = augment_discovery_plan_with_exact_names(discovery, matches)

    assert augmented.rows[0].candidate_ciks == (123,)
    assert augmented.rows[0].candidate_evidence_pointers == (
        "sec-fsds://source/accession",
    )


def test_fsds_name_discovery_cannot_use_a_post_cutoff_filing(tmp_path) -> None:
    filings = tmp_path / "filings.parquet"
    pq.write_table(
        pa.table(
            {
                "cik": pa.array([100, 200], type=pa.int64()),
                "name": ["Available Corp", "Future Corp"],
                "source_sha256": ["a" * 64, "a" * 64],
                "adsh": ["0000000100-26-000001", "0000000200-26-000001"],
                "accepted": pa.array(
                    [
                        datetime(2026, 7, 17, 19, tzinfo=UTC),
                        datetime(2026, 7, 17, 21, tzinfo=UTC),
                    ],
                    type=pa.timestamp("us", tz="UTC"),
                ),
            }
        ),
        filings,
    )
    batch = SimpleNamespace(table_path=lambda _name: filings)

    observations = fsds_company_name_observations(
        (batch,), as_of=datetime(2026, 7, 17, 20, tzinfo=UTC)
    )

    assert [(row.cik, row.name) for row in observations] == [(100, "Available Corp")]
    assert observations[0].evidence_pointer.endswith("?cik=100#issuer-name")
