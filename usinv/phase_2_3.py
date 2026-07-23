"""End-to-end composition for the real Phase 2.3 universe acceptance gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.config import AppConfig
from usinv.data.edgar.applicability import (
    ApplicabilityCoverageReport,
    build_applicability_coverage,
    cover_share_evidence,
    derive_structural_absence_evidence,
    observed_standardized_evidence,
)
from usinv.data.edgar.cover_shards import CoverEvidenceSnapshot
from usinv.data.edgar.filing_sic_snapshot import FilingSicSnapshot
from usinv.data.edgar.fsds import FsdsIngestResult
from usinv.data.edgar.live_edge import LiveEdgeSnapshot
from usinv.data.edgar.pit_store import PitStoreResult
from usinv.data.edgar.quarterly import derive_quarterly_facts
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan
from usinv.data.edgar.tag_chains import load_structural_identity_facts, standardize_pit_snapshot
from usinv.data.edgar.ttm import build_ttm_facts
from usinv.data.listings import AlphaListingSnapshot
from usinv.data.prices.universe import PriceUniverseSnapshot
from usinv.data.tiingo_lifecycle import TiingoLifecycleSnapshot
from usinv.data.universe_evidence import (
    FilingSicObservation,
    UniverseEvidenceBuild,
    build_security_universe_evidence,
    filing_sic_observations,
)
from usinv.universe import (
    IdentityRegimeEvidence,
    UniverseSnapshot,
    build_universe_snapshot,
)


class Phase23BuildError(ValueError):
    """Raised when the final gate inputs do not belong to one exact evidence lineage."""


def _validate_supplements_as_of(
    supplements: tuple[LiveEdgeSnapshot, ...], cutoff: datetime
) -> None:
    """Reject, rather than silently filter, live-edge facts beyond the signal cutoff."""

    for snapshot in supplements:
        try:
            accepted = pq.read_table(
                snapshot.output_dir / "filing_facts.parquet", columns=["accepted"]
            ).column("accepted")
        except (OSError, pa.ArrowException) as exc:
            raise Phase23BuildError("live-edge supplement is unreadable") from exc
        if accepted.null_count or any(value.as_py() > cutoff for value in accepted):
            raise Phase23BuildError("live-edge supplement contains post-cutoff facts")


def _supplement_sic_observations(
    supplements: tuple[LiveEdgeSnapshot, ...], cutoff: datetime
) -> tuple[FilingSicObservation, ...]:
    rows: list[FilingSicObservation] = []
    for snapshot in supplements:
        if snapshot.filing_sic is None:
            continue
        if (
            snapshot.cik is None
            or snapshot.accession is None
            or snapshot.accepted is None
            or snapshot.accepted.astimezone(UTC) > cutoff
            or snapshot.filing_sic_evidence_pointer is None
        ):
            raise Phase23BuildError("live-edge SIC provenance is incomplete or post-cutoff")
        rows.append(
            FilingSicObservation(
                snapshot.cik,
                snapshot.accession,
                snapshot.filing_sic,
                snapshot.accepted,
                snapshot.filing_sic_evidence_pointer,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.cik, row.accepted, row.accession)))


def _filing_sic_snapshot_observations(
    supplements: tuple[FilingSicSnapshot, ...],
    cutoff: datetime,
    cover_snapshot_id: str,
) -> tuple[FilingSicObservation, ...]:
    rows: list[FilingSicObservation] = []
    for snapshot in supplements:
        if (
            snapshot.cover_snapshot_id != cover_snapshot_id
            or snapshot.as_of.astimezone(UTC) != cutoff
        ):
            raise Phase23BuildError("filing-SIC supplement lineage or cutoff differs")
        rows.extend(
            FilingSicObservation(
                record.cik,
                record.accession,
                record.sic,
                record.accepted,
                (
                    f"{record.source_url}#standard-industrial-classification"
                    f";sha256={record.source_sha256}"
                    f";header_sha256={record.header_sha256}"
                ),
            )
            for record in snapshot.records
        )
    return tuple(sorted(rows, key=lambda row: (row.cik, row.accepted, row.accession)))


def _identity_regime_evidence(
    discovery: FilingDiscoveryPlan,
    cover: CoverEvidenceSnapshot,
    cutoff: datetime,
) -> IdentityRegimeEvidence:
    """Distill archived filer-regime evidence for otherwise-unmapped listings.

    Foreign classification requires that every archived cover filing for the
    CIK is a known foreign-form accession, so an old foreign registration can
    never reclassify a domestic 10-K/10-Q filer. 40-F-only filers produce no
    foreign-form observations under the current acquisition contract and
    therefore stay unmapped until the acquisition layer records them.
    """
    merge = cover.merge
    security_ciks = {security.cik for security in merge.master.securities}
    security_regimes: dict[int, set[bool]] = {}
    for security in merge.master.securities:
        security_regimes.setdefault(security.cik, set()).add(security.domestic_flag)
    filing_proven_foreign_ciks = {
        cik for cik, regimes in security_regimes.items() if regimes == {False}
    }
    archives_by_cik: dict[int, set[str]] = {}
    for archive in merge.archives:
        archives_by_cik.setdefault(archive.cik, set()).add(archive.accession)
    foreign_accessions: dict[int, set[str]] = {}
    foreign_pointers: dict[int, set[str]] = {}
    for observation in merge.fpi_form_observations:
        foreign_accessions.setdefault(observation.cik, set()).add(observation.accession)
        if observation.accepted.astimezone(UTC) <= cutoff:
            foreign_pointers.setdefault(observation.cik, set()).add(
                observation.evidence_pointer
            )
    foreign_regime = {
        cik: tuple(sorted(pointers))
        for cik, pointers in foreign_pointers.items()
        if (
            archives_by_cik.get(cik, set()) <= foreign_accessions.get(cik, set())
            or cik in filing_proven_foreign_ciks
        )
    }
    no_periodic = {
        proof.cik: (proof.evidence_pointer,)
        for proof in merge.form_history_proofs
        if proof.cik not in security_ciks
        and proof.cik not in archives_by_cik
        and proof.cik not in foreign_accessions
    }
    proofs_by_cik = {
        proof.cik: proof.evidence_pointer for proof in merge.form_history_proofs
    }
    securities = {security.security_id: security for security in merge.master.securities}
    current_common_pointers: dict[int, set[str]] = {}
    for symbol in merge.master.symbols:
        security = securities[symbol.security_id]
        if (
            security.security_type == "common_stock"
            and symbol.contains(cutoff.date())
            and symbol.known_at.astimezone(UTC) <= cutoff
            and symbol.known_at.astimezone(UTC) >= cutoff - timedelta(days=400)
            and symbol.confidence == "high"
            and symbol.scope == "historical_interval"
        ):
            current_common_pointers.setdefault(security.cik, set()).add(
                symbol.evidence_pointer
            )
    superseded_by_listing: dict[str, tuple[str, ...]] = {}
    for row in discovery.rows:
        if (
            len(row.candidate_ciks) != 1
            or row.normalized_exchange is None
            or not row.candidate_evidence_pointers
            or any(
                "confidence=weak" in pointer
                for pointer in row.candidate_evidence_pointers
            )
        ):
            continue
        cik = row.candidate_ciks[0]
        if (
            cik not in proofs_by_cik
            or len(archives_by_cik.get(cik, ())) < 2
            or cik not in current_common_pointers
        ):
            continue
        mapping = merge.master.resolve(
            row.ticker,
            row.normalized_exchange,
            cutoff.date(),
            minimum_confidence="high",
            required_security_type="common_stock",
        )
        if mapping.status == "unmapped":
            superseded_by_listing[row.listing_evidence_pointer] = tuple(
                sorted(
                    {
                        *row.candidate_evidence_pointers,
                        proofs_by_cik[cik],
                        *current_common_pointers[cik],
                    }
                )
            )
    return IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            row.listing_evidence_pointer: row.candidate_ciks
            for row in discovery.rows
            if row.candidate_ciks
            and not any(
                "confidence=weak" in pointer
                for pointer in row.candidate_evidence_pointers
            )
        },
        foreign_regime_pointers=foreign_regime,
        no_periodic_pointers=no_periodic,
        superseded_pointers_by_listing=superseded_by_listing,
    )


@dataclass(frozen=True, slots=True)
class Phase23Build:
    universe: UniverseSnapshot
    coverage: ApplicabilityCoverageReport
    evidence: UniverseEvidenceBuild
    standardized_facts: int
    quarterly_facts: int
    quarterly_quarantine: int
    ttm_facts: int


def _active_listing_matches_discovery(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
) -> bool:
    """Verify exact active CSV content while allowing a later retrieval timestamp."""
    active = tuple(
        (
            f"alpha-vantage://{row.source_sha256}/{row.row_number}",
            row.exchange,
            row.asset_type,
        )
        for row in sorted(
            (row for row in listing.rows if row.state == "active"),
            key=lambda row: (row.exchange, row.symbol, row.row_number),
        )
    )
    planned = tuple(
        (row.listing_evidence_pointer, row.raw_exchange, row.asset_type) for row in discovery.rows
    )
    return bool(active) and active == planned


def build_phase_2_3(
    listing: AlphaListingSnapshot,
    discovery: FilingDiscoveryPlan,
    cover: CoverEvidenceSnapshot,
    prices: PriceUniverseSnapshot,
    ingested: tuple[FsdsIngestResult, ...],
    pit: PitStoreResult,
    *,
    signal_at: datetime,
    config: AppConfig,
    lifecycle: TiingoLifecycleSnapshot | None = None,
    supplements: tuple[LiveEdgeSnapshot, ...] = (),
    filing_sic_supplements: tuple[FilingSicSnapshot, ...] = (),
) -> Phase23Build:
    """Build, but do not weaken or auto-pass, the real D032 acceptance denominator."""
    if signal_at.tzinfo is None:
        raise Phase23BuildError("Phase 2.3 signal must be timezone-aware")
    cutoff = signal_at.astimezone(UTC)
    if (
        listing.as_of != discovery.listing_as_of
        or not _active_listing_matches_discovery(listing, discovery)
        or listing.as_of != signal_at.date()
        or cover.merge.as_of.astimezone(UTC) != cutoff
        or prices.plan.signal_at.astimezone(UTC) != cutoff
        or prices.plan.discovery_snapshot_id != discovery.snapshot_id
        or prices.plan.cover_snapshot_id != cover.snapshot_id
        or not ingested
        or tuple(
            sorted(
                [
                    *(row.batch_id for row in ingested),
                    *(row.batch_id for row in supplements),
                ]
            )
        )
        != tuple(sorted(row.batch_id for row in pit.inputs))
    ):
        raise Phase23BuildError("Phase 2.3 inputs do not share one exact lineage")
    _validate_supplements_as_of(supplements, cutoff)

    presentation_paths = (
        *(row.table_path("raw_pre") for row in ingested),
        *(row.output_dir / "presentation.parquet" for row in supplements),
    )
    standardized = standardize_pit_snapshot(
        pit.table_path("facts_pit"),
        presentation_paths=presentation_paths,
    )
    quarterly = derive_quarterly_facts(standardized)
    ttm = build_ttm_facts(quarterly.facts)
    sic = (
        *filing_sic_observations(ingested, as_of=cutoff),
        *_supplement_sic_observations(supplements, cutoff),
        *_filing_sic_snapshot_observations(
            filing_sic_supplements,
            cutoff,
            cover.snapshot_id,
        ),
    )
    evidence = build_security_universe_evidence(
        cover,
        prices.price_snapshots,
        sic,
        ttm,
        signal_at=cutoff,
    )
    universe = build_universe_snapshot(
        listing,
        cover.merge.master,
        evidence.evidence,
        signal_at=cutoff,
        config=config.universe,
        config_hash=config.config_hash,
        security_master_snapshot_id=cover.master_snapshot_id,
        lifecycle=lifecycle,
        regime=_identity_regime_evidence(discovery, cover, cutoff),
    )
    identity_facts = load_structural_identity_facts(pit.table_path("facts_pit"))
    coverage = build_applicability_coverage(
        cover.merge.master,
        universe.applicability_candidates(),
        (
            *observed_standardized_evidence(standardized),
            *derive_structural_absence_evidence(identity_facts, standardized, as_of=cutoff),
            *cover_share_evidence(cover.merge.share_observations, as_of=cutoff),
        ),
        as_of=cutoff,
        mandatory_concepts=("revenue", "net_income"),
    )
    return Phase23Build(
        universe,
        coverage,
        evidence,
        len(standardized),
        len(quarterly.facts),
        len(quarterly.quarantine),
        len(ttm),
    )
