"""End-to-end composition for the real Phase 2.3 universe acceptance gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from usinv.config import AppConfig
from usinv.data.edgar.applicability import (
    ApplicabilityCoverageReport,
    build_applicability_coverage,
    observed_standardized_evidence,
)
from usinv.data.edgar.cover_shards import CoverEvidenceSnapshot
from usinv.data.edgar.fsds import FsdsIngestResult
from usinv.data.edgar.pit_store import PitStoreResult
from usinv.data.edgar.quarterly import derive_quarterly_facts
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan
from usinv.data.edgar.tag_chains import standardize_pit_snapshot
from usinv.data.edgar.ttm import build_ttm_facts
from usinv.data.listings import AlphaListingSnapshot
from usinv.data.prices.universe import PriceUniverseSnapshot
from usinv.data.universe_evidence import (
    UniverseEvidenceBuild,
    build_security_universe_evidence,
    filing_sic_observations,
)
from usinv.universe import UniverseSnapshot, build_universe_snapshot


class Phase23BuildError(ValueError):
    """Raised when the final gate inputs do not belong to one exact evidence lineage."""


@dataclass(frozen=True, slots=True)
class Phase23Build:
    universe: UniverseSnapshot
    coverage: ApplicabilityCoverageReport
    evidence: UniverseEvidenceBuild
    standardized_facts: int
    quarterly_facts: int
    quarterly_quarantine: int
    ttm_facts: int


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
) -> Phase23Build:
    """Build, but do not weaken or auto-pass, the real D032 acceptance denominator."""
    if signal_at.tzinfo is None:
        raise Phase23BuildError("Phase 2.3 signal must be timezone-aware")
    cutoff = signal_at.astimezone(UTC)
    if (
        listing.snapshot_id != discovery.listing_snapshot_id
        or listing.as_of != discovery.listing_as_of
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
    )
    coverage = build_applicability_coverage(
        cover.merge.master,
        universe.applicability_candidates(),
        observed_standardized_evidence(standardized),
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
