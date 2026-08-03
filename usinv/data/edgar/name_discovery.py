"""Conservative company-name matching for SEC CIK discovery only.

The matches produced here are not historical security identity evidence.  A
caller must still corroborate the candidate CIK with point-in-time filing
evidence before creating or excluding a security.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

import duckdb

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.fsds import FsdsIngestResult
from usinv.data.edgar.security_bootstrap import DISCOVERY_VERSION, FilingDiscoveryPlan
from usinv.data.listings import AlphaListingSnapshot

NameMatchStatus = Literal["blank", "unmatched", "unique", "ambiguous"]
_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holding",
        "holdings",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
    }
)


@dataclass(frozen=True, slots=True)
class EdgarCompanyName:
    """One name-to-CIK observation with an immutable evidence pointer."""

    cik: int
    name: str
    evidence_pointer: str

    def __post_init__(self) -> None:
        if self.cik <= 0 or not self.name.strip() or not self.evidence_pointer.strip():
            raise ValueError("EDGAR company-name evidence is incomplete")


@dataclass(frozen=True, slots=True)
class ExactNameMatch:
    """A discovery result that deliberately carries no identity confidence."""

    normalized_name: str
    status: NameMatchStatus
    candidate_ciks: tuple[int, ...]
    evidence_pointers: tuple[str, ...]

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.candidate_ciks))) != self.candidate_ciks:
            raise ValueError("name-discovery CIK candidates are not canonical")
        if tuple(sorted(set(self.evidence_pointers))) != self.evidence_pointers:
            raise ValueError("name-discovery evidence pointers are not canonical")
        expected = {
            "blank": 0,
            "unmatched": 0,
            "unique": 1,
        }.get(self.status)
        if expected is not None and len(self.candidate_ciks) != expected:
            raise ValueError("name-discovery status contradicts its CIK candidates")
        if self.status == "ambiguous" and len(self.candidate_ciks) < 2:
            raise ValueError("ambiguous name discovery needs multiple CIK candidates")
        if bool(self.candidate_ciks) != bool(self.evidence_pointers):
            raise ValueError("name-discovery candidates require evidence pointers")


def normalize_company_name(value: str) -> str:
    """Normalize only presentation differences, never corporate semantics.

    Legal suffixes and words are intentionally retained.  Treating ``Inc`` and
    ``LLC`` (or two genuinely different issuers with a shared stem) as
    interchangeable would be unsafe even for discovery.
    """

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in decomposed if character.isalnum())


def normalize_company_stem(value: str) -> str:
    """Return a suffix-insensitive discovery key, never an identity assertion."""

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    tokens = [
        "".join(character for character in token if character.isalnum())
        for token in decomposed.split()
    ]
    tokens = [token for token in tokens if token]
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    while tokens and tokens[-1] == "the":
        tokens.pop()
    if len(tokens) >= 2 and tokens[-2] == "class" and len(tokens[-1]) == 1:
        del tokens[-2:]
    while tokens and tokens[-1] in _LEGAL_SUFFIX_TOKENS:
        tokens.pop()
    return "".join(tokens)


def match_exact_company_name(
    listing_name: str,
    observations: Iterable[EdgarCompanyName],
) -> ExactNameMatch:
    """Return exact-normalized CIK candidates without asserting identity."""

    normalized = normalize_company_name(listing_name)
    if not normalized:
        return ExactNameMatch("", "blank", (), ())

    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for observation in observations:
        index[normalize_company_name(observation.name)][observation.cik].add(
            observation.evidence_pointer
        )
    return _match_indexed(normalized, index)


def _match_indexed(
    normalized: str, index: dict[str, dict[int, set[str]]]
) -> ExactNameMatch:
    if not normalized:
        return ExactNameMatch("", "blank", (), ())
    matches = index.get(normalized, {})
    ciks = tuple(sorted(matches))
    pointers = tuple(sorted({pointer for values in matches.values() for pointer in values}))
    if not ciks:
        status: NameMatchStatus = "unmatched"
    elif len(ciks) == 1:
        status = "unique"
    else:
        status = "ambiguous"
    return ExactNameMatch(normalized, status, ciks, pointers)


def fsds_company_name_observations(
    ingested: Iterable[FsdsIngestResult], *, as_of: datetime
) -> tuple[EdgarCompanyName, ...]:
    """Load PIT-bounded issuer names from verified FSDS filing artifacts."""

    if as_of.tzinfo is None:
        raise EdgarPayloadError("company-name discovery cutoff must be timezone-aware")
    batches = tuple(ingested)
    if not batches:
        raise EdgarPayloadError("company-name discovery requires FSDS filing evidence")
    paths = [str(batch.table_path("filings")) for batch in batches]
    cutoff = as_of.astimezone(UTC)
    connection = duckdb.connect(database=":memory:")
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT cik, trim(name), source_sha256, adsh
            FROM read_parquet(?)
            WHERE accepted <= ? AND cik > 0 AND name IS NOT NULL AND trim(name) != ''
            ORDER BY cik, trim(name), source_sha256, adsh
            """,
            [paths, cutoff],
        ).fetchall()
    except duckdb.Error as exc:
        raise EdgarPayloadError("FSDS company-name evidence is unreadable") from exc
    finally:
        connection.close()
    return tuple(
        EdgarCompanyName(
            int(cik),
            str(name),
            f"sec-fsds://{source_sha256}/{adsh}?cik={int(cik)}#issuer-name",
        )
        for cik, name, source_sha256, adsh in rows
    )


def match_unmapped_listing_names(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
    observations: Iterable[EdgarCompanyName],
) -> dict[str, ExactNameMatch]:
    """Match each unmapped active listing name to PIT-bounded EDGAR names."""

    listing_names = {
        f"alpha-vantage://{row.source_sha256}/{row.row_number}": row.name
        for row in listing.rows
        if row.state == "active"
    }
    planned_pointers = {row.listing_evidence_pointer for row in discovery.rows}
    if listing.as_of != discovery.listing_as_of or not planned_pointers <= set(listing_names):
        raise EdgarPayloadError("name discovery inputs do not share the listing lineage")
    evidence = tuple(observations)
    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for observation in evidence:
        index[normalize_company_name(observation.name)][observation.cik].add(
            observation.evidence_pointer
        )
    output: dict[str, ExactNameMatch] = {}
    for row in discovery.rows:
        if row.status != "unmapped":
            continue
        name = listing_names.get(row.listing_evidence_pointer)
        if name is None:
            raise EdgarPayloadError("unmapped discovery row has no active listing evidence")
        output[row.listing_evidence_pointer] = _match_indexed(
            normalize_company_name(name), index
        )
    return output


def match_discovered_listing_names(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
    observations: Iterable[EdgarCompanyName],
) -> dict[str, ExactNameMatch]:
    """Corroborate association-only CIK candidates with PIT issuer names.

    The SEC ticker association is useful for discovery but is not historical
    identity evidence. An exact, unique FSDS issuer-name match to the same CIK
    adds filing-time provenance without changing the candidate itself.
    """

    listing_names = {
        f"alpha-vantage://{row.source_sha256}/{row.row_number}": row.name
        for row in listing.rows
        if row.state == "active"
    }
    planned_pointers = {row.listing_evidence_pointer for row in discovery.rows}
    if listing.as_of != discovery.listing_as_of or not planned_pointers <= set(listing_names):
        raise EdgarPayloadError("name discovery inputs do not share the listing lineage")
    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for observation in observations:
        index[normalize_company_name(observation.name)][observation.cik].add(
            observation.evidence_pointer
        )
    output: dict[str, ExactNameMatch] = {}
    for row in discovery.rows:
        if (
            row.status != "discovered"
            or len(row.candidate_ciks) != 1
            or row.candidate_evidence_pointers
        ):
            continue
        name = listing_names.get(row.listing_evidence_pointer)
        if name is None:
            raise EdgarPayloadError("discovered row has no active listing evidence")
        output[row.listing_evidence_pointer] = _match_indexed(
            normalize_company_name(name), index
        )
    return output


def match_discovered_listing_stems(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
    observations: Iterable[EdgarCompanyName],
) -> dict[str, ExactNameMatch]:
    """Corroborate association-only candidates by a unique PIT legal-name stem."""

    listing_names = {
        f"alpha-vantage://{row.source_sha256}/{row.row_number}": row.name
        for row in listing.rows
        if row.state == "active"
    }
    planned_pointers = {row.listing_evidence_pointer for row in discovery.rows}
    if listing.as_of != discovery.listing_as_of or not planned_pointers <= set(listing_names):
        raise EdgarPayloadError("name discovery inputs do not share the listing lineage")
    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for observation in observations:
        index[normalize_company_stem(observation.name)][observation.cik].add(
            observation.evidence_pointer
        )
    output: dict[str, ExactNameMatch] = {}
    for row in discovery.rows:
        if (
            row.status != "discovered"
            or len(row.candidate_ciks) != 1
            or row.candidate_evidence_pointers
        ):
            continue
        name = listing_names.get(row.listing_evidence_pointer)
        if name is None:
            raise EdgarPayloadError("discovered row has no active listing evidence")
        output[row.listing_evidence_pointer] = _match_indexed(
            normalize_company_stem(name), index
        )
    return output


def match_unmapped_listing_stems(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
    observations: Iterable[EdgarCompanyName],
) -> dict[str, ExactNameMatch]:
    """Find suffix-insensitive CIK candidates for filing corroboration only."""

    listing_names = {
        f"alpha-vantage://{row.source_sha256}/{row.row_number}": row.name
        for row in listing.rows
        if row.state == "active"
    }
    planned_pointers = {row.listing_evidence_pointer for row in discovery.rows}
    if listing.as_of != discovery.listing_as_of or not planned_pointers <= set(listing_names):
        raise EdgarPayloadError("name discovery inputs do not share the listing lineage")
    index: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    for observation in observations:
        index[normalize_company_stem(observation.name)][observation.cik].add(
            observation.evidence_pointer
        )
    output: dict[str, ExactNameMatch] = {}
    for row in discovery.rows:
        if row.status != "unmapped":
            continue
        name = listing_names.get(row.listing_evidence_pointer)
        if name is None:
            raise EdgarPayloadError("unmapped discovery row has no active listing evidence")
        output[row.listing_evidence_pointer] = _match_indexed(
            normalize_company_stem(name), index
        )
    return output


def augment_discovery_plan_with_exact_name_evidence(
    discovery: FilingDiscoveryPlan,
    matches: dict[str, ExactNameMatch],
) -> FilingDiscoveryPlan:
    """Attach exact filing-name provenance to an unchanged unique candidate."""

    expected = {
        row.listing_evidence_pointer
        for row in discovery.rows
        if row.status == "discovered"
        and len(row.candidate_ciks) == 1
        and not row.candidate_evidence_pointers
    }
    if set(matches) != expected:
        raise EdgarPayloadError(
            "name corroborations do not cover association-only rows exactly"
        )
    rows = tuple(
        replace(
            row,
            candidate_evidence_pointers=matches[
                row.listing_evidence_pointer
            ].evidence_pointers,
        )
        if row.listing_evidence_pointer in expected
        and matches[row.listing_evidence_pointer].status == "unique"
        and matches[row.listing_evidence_pointer].candidate_ciks == row.candidate_ciks
        else row
        for row in discovery.rows
    )
    return FilingDiscoveryPlan(
        discovery.listing_as_of,
        discovery.listing_snapshot_id,
        discovery.association_source_sha256,
        discovery.association_observed_at,
        rows,
        version=DISCOVERY_VERSION,
        association_unusable_rows=discovery.association_unusable_rows,
    )


def augment_discovery_plan_with_exact_names(
    discovery: FilingDiscoveryPlan,
    matches: dict[str, ExactNameMatch],
) -> FilingDiscoveryPlan:
    """Promote bounded name matches to discovery candidates, never identities."""

    expected = {
        row.listing_evidence_pointer
        for row in discovery.rows
        if row.status == "unmapped"
    }
    if set(matches) != expected:
        raise EdgarPayloadError("name matches do not cover the plan's unmapped rows exactly")
    rows = tuple(
        replace(
            row,
            status=(
                "discovered"
                if matches[row.listing_evidence_pointer].status == "unique"
                else "ambiguous"
            ),
            candidate_ciks=matches[row.listing_evidence_pointer].candidate_ciks,
            candidate_evidence_pointers=matches[
                row.listing_evidence_pointer
            ].evidence_pointers,
        )
        if row.status == "unmapped"
        and matches[row.listing_evidence_pointer].status in {"unique", "ambiguous"}
        else row
        for row in discovery.rows
    )
    return FilingDiscoveryPlan(
        discovery.listing_as_of,
        discovery.listing_snapshot_id,
        discovery.association_source_sha256,
        discovery.association_observed_at,
        rows,
        version=DISCOVERY_VERSION,
        association_unusable_rows=discovery.association_unusable_rows,
    )
