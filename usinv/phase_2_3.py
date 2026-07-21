"""End-to-end composition for the real Phase 2.3 universe acceptance gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from usinv.config import AppConfig
from usinv.data.edgar.applicability import (
    ApplicabilityCoverageReport,
    build_applicability_coverage,
    cover_share_evidence,
    derive_structural_absence_evidence,
    observed_standardized_evidence,
)
from usinv.data.edgar.cover_shards import CoverEvidenceSnapshot
from usinv.data.edgar.fsds import FsdsIngestResult
from usinv.data.edgar.pit_store import PitStoreResult
from usinv.data.edgar.quarterly import derive_quarterly_facts
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan
from usinv.data.edgar.tag_chains import load_structural_identity_facts, standardize_pit_snapshot
from usinv.data.edgar.ttm import build_ttm_facts
from usinv.data.listings import AlphaListingSnapshot
from usinv.data.prices.universe import PriceUniverseSnapshot
from usinv.data.tiingo_lifecycle import TiingoLifecycleSnapshot
from usinv.data.universe_evidence import (
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
        if cik not in security_ciks
        and archives_by_cik.get(cik, set()) <= foreign_accessions.get(cik, set())
    }
    no_periodic = {
        proof.cik: (proof.evidence_pointer,)
        for proof in merge.form_history_proofs
        if proof.cik not in security_ciks
        and proof.cik not in archives_by_cik
        and proof.cik not in foreign_accessions
    }
    return IdentityRegimeEvidence(
        candidate_ciks_by_pointer={
            row.listing_evidence_pointer: row.candidate_ciks
            for row in discovery.rows
            if row.candidate_ciks
        },
        foreign_regime_pointers=foreign_regime,
        no_periodic_pointers=no_periodic,
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
        or tuple(sorted(row.batch_id for row in ingested))
        != tuple(sorted(row.batch_id for row in pit.inputs))
    ):
        raise Phase23BuildError("Phase 2.3 inputs do not share one exact lineage")

    presentation_paths = tuple(row.table_path("raw_pre") for row in ingested)
    standardized = standardize_pit_snapshot(
        pit.table_path("facts_pit"),
        presentation_paths=presentation_paths,
    )
    quarterly = derive_quarterly_facts(standardized)
    ttm = build_ttm_facts(quarterly.facts)
    sic = filing_sic_observations(ingested, as_of=cutoff)
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
