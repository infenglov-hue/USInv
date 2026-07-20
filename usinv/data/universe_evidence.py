"""Verified adapters that compose Phase 2.3 point-in-time universe evidence."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from usinv.data.edgar.cover_shards import (
    CoverEvidenceSnapshot,
    read_cover_evidence_snapshot,
)
from usinv.data.edgar.fsds import FILINGS_SCHEMA, FsdsIngestResult
from usinv.data.edgar.ttm import TtmFact
from usinv.data.prices.base import (
    PRICES_RAW_SCHEMA,
    PriceSnapshot,
    read_price_snapshot,
)
from usinv.universe import (
    PRE_REVENUE_BIOTECH_SICS,
    FilingFormObservation,
    SecurityUniverseEvidence,
    UniversePriceBar,
)

UNIVERSE_EVIDENCE_VERSION: Final = "usinv-universe-evidence-v1"
_PRE_REVENUE_MAX = Decimal("10000000")


class UniverseEvidenceError(ValueError):
    """Raised when verified source artifacts cannot be composed without leakage."""


@dataclass(frozen=True, slots=True)
class FilingSicObservation:
    cik: int
    accession: str
    sic: int
    accepted: datetime
    evidence_pointer: str

    def __post_init__(self) -> None:
        if (
            self.cik <= 0
            or not self.accession
            or isinstance(self.sic, bool)
            or not 100 <= self.sic <= 9999
            or self.accepted.tzinfo is None
            or not self.evidence_pointer
        ):
            raise UniverseEvidenceError("filing SIC observation is invalid")


@dataclass(frozen=True, slots=True)
class UniverseEvidenceGap:
    security_id: str
    cik: int
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class UniverseEvidenceBuild:
    cover_snapshot_id: str
    price_snapshot_id: str
    signal_at: datetime
    evidence: tuple[SecurityUniverseEvidence, ...]
    gaps: tuple[UniverseEvidenceGap, ...]
    version: str = UNIVERSE_EVIDENCE_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def filing_sic_observations(
    results: Iterable[FsdsIngestResult],
    *,
    as_of: datetime,
) -> tuple[FilingSicObservation, ...]:
    """Read filing-time SIC values only from hash-verified FSDS ingest results."""
    if as_of.tzinfo is None:
        raise UniverseEvidenceError("SIC cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    by_accession: dict[str, FilingSicObservation] = {}
    seen_batches: set[str] = set()
    for result in sorted(results, key=lambda item: item.batch_id):
        if result.batch_id in seen_batches:
            raise UniverseEvidenceError("duplicate FSDS filing batch")
        seen_batches.add(result.batch_id)
        artifact = next((row for row in result.tables if row.name == "filings"), None)
        if artifact is None:
            raise UniverseEvidenceError("FSDS result has no filings artifact")
        path = result.table_path("filings")
        if (
            _sha256(path) != artifact.content_sha256
            or not pq.ParquetFile(path).schema_arrow.equals(FILINGS_SCHEMA, check_metadata=True)
        ):
            raise UniverseEvidenceError("FSDS filings artifact failed verification")
        rows = pq.read_table(
            path,
            columns=["source_quarter", "source_sha256", "adsh", "cik", "sic", "accepted"],
        ).to_pylist()
        for row in rows:
            if row["sic"] is None or row["accepted"].astimezone(UTC) > cutoff:
                continue
            observation = FilingSicObservation(
                row["cik"],
                row["adsh"],
                row["sic"],
                row["accepted"],
                (
                    f"sec-fsds://{row['source_quarter']}/{row['source_sha256']}"
                    f"/filings/{row['adsh']}#sic"
                ),
            )
            prior = by_accession.get(observation.accession)
            if prior is not None and prior != observation:
                raise UniverseEvidenceError(
                    f"conflicting filing SIC evidence for {observation.accession}"
                )
            by_accession[observation.accession] = observation
    return tuple(
        sorted(by_accession.values(), key=lambda row: (row.cik, row.accepted, row.accession))
    )


def _latest_share(rows, cutoff: datetime):
    eligible = [row for row in rows if row.accepted.astimezone(UTC) <= cutoff]
    if not eligible:
        return None, False
    latest_at = max(row.accepted.astimezone(UTC) for row in eligible)
    latest = [row for row in eligible if row.accepted.astimezone(UTC) == latest_at]
    values = {row.shares_outstanding for row in latest}
    if len(values) != 1:
        return None, True
    return min(latest, key=lambda row: row.evidence_pointer), False


def _latest_sic(rows: list[FilingSicObservation], cutoff: datetime):
    eligible = [row for row in rows if row.accepted.astimezone(UTC) <= cutoff]
    if not eligible:
        return None, False
    latest_at = max(row.accepted.astimezone(UTC) for row in eligible)
    latest = [row for row in eligible if row.accepted.astimezone(UTC) == latest_at]
    if len({row.sic for row in latest}) != 1:
        return None, True
    return min(latest, key=lambda row: row.evidence_pointer), False


def _latest_revenue(rows: list[TtmFact], cutoff: datetime):
    eligible = [
        row
        for row in rows
        if row.concept == "revenue" and row.available_from.astimezone(UTC) <= cutoff
    ]
    if not eligible:
        return None, False
    latest_period = max(row.period_end for row in eligible)
    latest = [row for row in eligible if row.period_end == latest_period]
    latest_at = max(row.available_from.astimezone(UTC) for row in latest)
    final = [row for row in latest if row.available_from.astimezone(UTC) == latest_at]
    if len({(row.uom, row.value) for row in final}) != 1:
        return None, True
    return min(final, key=lambda row: (row.source_adshs, row.source_tags)), False


def build_security_universe_evidence(
    cover_snapshot: CoverEvidenceSnapshot,
    price_snapshot: PriceSnapshot,
    sic_observations: Iterable[FilingSicObservation],
    ttm_facts: Iterable[TtmFact],
    *,
    signal_at: datetime,
) -> UniverseEvidenceBuild:
    """Join class shares, raw prices, filing forms, SIC and biotech evidence by stable IDs."""
    if signal_at.tzinfo is None:
        raise UniverseEvidenceError("universe evidence cutoff must be timezone-aware")
    cutoff = signal_at.astimezone(UTC)
    verified_cover = read_cover_evidence_snapshot(cover_snapshot.output_dir)
    verified_prices = read_price_snapshot(price_snapshot.output_dir)
    if (
        verified_cover.snapshot_id != cover_snapshot.snapshot_id
        or verified_prices.snapshot_id != price_snapshot.snapshot_id
        or verified_cover.merge.as_of.astimezone(UTC) != cutoff
    ):
        raise UniverseEvidenceError("universe evidence artifact identities or cutoff differ")
    master = verified_cover.merge.master
    securities = {row.security_id: row for row in master.securities}

    price_table = pq.read_table(verified_prices.output_dir / "prices_raw.parquet")
    if not price_table.schema.equals(PRICES_RAW_SCHEMA, check_metadata=True):
        raise UniverseEvidenceError("raw price evidence schema is invalid")
    prices_by_security: dict[str, dict[object, UniversePriceBar]] = defaultdict(dict)
    for row in price_table.to_pylist():
        if row["security_id"] not in securities or row["session"] > signal_at.date():
            continue
        pointer = (
            f"{row['source_url']}#{row['source_page_sha256']}"
            f":{row['batch_id']}:{row['security_id']}:{row['session'].isoformat()}"
        )
        bar = UniversePriceBar(row["session"], row["close"], row["volume"], pointer)
        prior = prices_by_security[row["security_id"]].get(row["session"])
        if prior is not None and prior != bar:
            raise UniverseEvidenceError("conflicting raw prices share a security/session key")
        prices_by_security[row["security_id"]][row["session"]] = bar

    shares_by_security: dict[str, list[object]] = defaultdict(list)
    for row in verified_cover.merge.share_observations:
        shares_by_security[row.security_id].append(row)
    fpi_by_cik: dict[int, list[object]] = defaultdict(list)
    for row in verified_cover.merge.fpi_form_observations:
        if row.accepted.astimezone(UTC) <= cutoff:
            fpi_by_cik[row.cik].append(row)
    proofs = {row.cik: row for row in verified_cover.merge.form_history_proofs}
    sic_by_cik: dict[int, list[FilingSicObservation]] = defaultdict(list)
    for row in sic_observations:
        sic_by_cik[row.cik].append(row)
    revenue_by_cik: dict[int, list[TtmFact]] = defaultdict(list)
    for row in ttm_facts:
        revenue_by_cik[row.cik].append(row)

    evidence: list[SecurityUniverseEvidence] = []
    gaps: list[UniverseEvidenceGap] = []
    for security in sorted(master.securities, key=lambda row: row.security_id):
        share, share_conflict = _latest_share(shares_by_security[security.security_id], cutoff)
        sic, sic_conflict = _latest_sic(sic_by_cik[security.cik], cutoff)
        proof = proofs.get(security.cik)
        if share is None:
            gaps.append(
                UniverseEvidenceGap(
                    security.security_id,
                    security.cik,
                    "conflicting_shares" if share_conflict else "missing_class_shares",
                    "no unambiguous class-specific shares are available at the cutoff",
                )
            )
        if sic is None:
            gaps.append(
                UniverseEvidenceGap(
                    security.security_id,
                    security.cik,
                    "conflicting_sic" if sic_conflict else "missing_filing_sic",
                    "no unambiguous filing-time SIC is available at the cutoff",
                )
            )
        if proof is None:
            gaps.append(
                UniverseEvidenceGap(
                    security.security_id,
                    security.cik,
                    "incomplete_form_history",
                    "SEC-declared submissions history is not completely archived",
                )
            )

        biotech = None
        biotech_available_from = None
        biotech_pointer = None
        if sic is not None and sic.sic in PRE_REVENUE_BIOTECH_SICS:
            revenue, revenue_conflict = _latest_revenue(revenue_by_cik[security.cik], cutoff)
            if revenue is None:
                gaps.append(
                    UniverseEvidenceGap(
                        security.security_id,
                        security.cik,
                        "conflicting_ttm_revenue" if revenue_conflict else "missing_ttm_revenue",
                        "pre-revenue biotech classification requires unambiguous TTM revenue",
                    )
                )
            else:
                biotech = revenue.value < _PRE_REVENUE_MAX
                biotech_available_from = revenue.available_from
                biotech_pointer = (
                    f"sec-ttm://{revenue.chain_version}/{security.cik}/"
                    f"{revenue.period_end.isoformat()}/" + ",".join(revenue.source_adshs)
                )

        form_rows = tuple(
            FilingFormObservation(row.form, row.accepted, row.evidence_pointer)
            for row in sorted(
                fpi_by_cik[security.cik],
                key=lambda row: (row.accepted, row.accession),
            )
        )
        price_rows = tuple(
            prices_by_security[security.security_id][session]
            for session in sorted(prices_by_security[security.security_id])
        )
        evidence.append(
            SecurityUniverseEvidence(
                security_id=security.security_id,
                price_bars=price_rows,
                shares_outstanding=share.shares_outstanding if share else None,
                shares_available_from=share.accepted if share else None,
                shares_evidence_pointer=share.evidence_pointer if share else None,
                sic=sic.sic if sic else None,
                sic_available_from=sic.accepted if sic else None,
                sic_evidence_pointer=sic.evidence_pointer if sic else None,
                form_history=form_rows,
                form_history_complete=proof is not None,
                form_history_evidence_pointer=proof.evidence_pointer if proof else None,
                pre_revenue_biotech=biotech,
                biotech_available_from=biotech_available_from,
                biotech_evidence_pointer=biotech_pointer,
            )
        )
    return UniverseEvidenceBuild(
        verified_cover.snapshot_id,
        verified_prices.snapshot_id,
        cutoff,
        tuple(evidence),
        tuple(sorted(gaps, key=lambda row: (row.security_id, row.kind))),
    )
