"""Resumable, fail-closed acquisition of filing-time security identity evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

from usinv.data.edgar.client import EdgarClient, EdgarHttpError, EdgarPayloadError
from usinv.data.edgar.filing_xbrl import (
    FilingParseResult,
    archive_filing,
    extract_cover_security_classes,
    parse_filing_xbrl,
    security_evidence_from_cover,
)
from usinv.data.edgar.securities import SecurityMasterError, normalize_exchange, normalize_ticker
from usinv.data.edgar.security_bootstrap import (
    CoverFilingEvidence,
    FilingDiscoveryPlan,
    select_cover_filings,
)
from usinv.data.edgar.submissions import (
    SubmissionFiling,
    SubmissionFormObservation,
    parse_submission_history_forms,
    parse_submissions_document,
)

_DOMESTIC_FORMS = frozenset({"10-K", "10-Q", "S-1"})
_FOREIGN_FORMS = frozenset({"20-F", "40-F", "F-1"})
_FPI_CLASSIFICATION_FORMS = frozenset({"20-F", "6-K", "F-1"})


@dataclass(frozen=True, slots=True)
class CoverAcquisitionGap:
    cik: int
    accession: str | None
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class CoverArchiveRecord:
    cik: int
    accession: str
    archive_snapshot_id: str
    primary_sha256: str


@dataclass(frozen=True, slots=True)
class CoverShareObservation:
    security_id: str
    cik: int
    accepted: datetime
    shares_outstanding: Decimal
    evidence_pointer: str

    def __post_init__(self) -> None:
        if (
            not self.security_id
            or self.cik <= 0
            or self.accepted.tzinfo is None
            or not self.shares_outstanding.is_finite()
            or self.shares_outstanding <= 0
            or not self.evidence_pointer
        ):
            raise EdgarPayloadError("cover share observation provenance is incomplete")


@dataclass(frozen=True, slots=True)
class CoverFpiFormObservation:
    cik: int
    accession: str
    form: str
    accepted: datetime
    evidence_pointer: str

    def __post_init__(self) -> None:
        if (
            self.cik <= 0
            or not self.accession
            or self.form.upper().removesuffix("/A") not in _FPI_CLASSIFICATION_FORMS
            or self.accepted.tzinfo is None
            or not self.evidence_pointer
        ):
            raise EdgarPayloadError("FPI form observation provenance is incomplete")


@dataclass(frozen=True, slots=True)
class CoverFormHistoryProof:
    cik: int
    as_of: datetime
    source_documents: tuple[str, ...]
    evidence_pointer: str

    def __post_init__(self) -> None:
        if (
            self.cik <= 0
            or self.as_of.tzinfo is None
            or not self.source_documents
            or tuple(sorted(set(self.source_documents))) != self.source_documents
            or not self.evidence_pointer
        ):
            raise EdgarPayloadError("form-history completion proof is invalid")


@dataclass(frozen=True, slots=True)
class CoverAcquisitionResult:
    plan_snapshot_id: str
    as_of: datetime
    requested_ciks: tuple[int, ...]
    deferred_ciks: tuple[int, ...]
    start_after_cik: int | None
    selected_filings: int
    archived_filings: int
    archives: tuple[CoverArchiveRecord, ...]
    share_observations: tuple[CoverShareObservation, ...]
    evidence: tuple[CoverFilingEvidence, ...]
    gaps: tuple[CoverAcquisitionGap, ...]
    fpi_form_observations: tuple[CoverFpiFormObservation, ...] = ()
    form_history_proofs: tuple[CoverFormHistoryProof, ...] = ()

    @property
    def complete(self) -> bool:
        return self.start_after_cik is None and not self.deferred_ciks


def infer_domestic_flag(
    filings: tuple[SubmissionFiling | SubmissionFormObservation, ...],
    *,
    as_of: datetime,
) -> bool | None:
    """Infer filer regime from the latest accepted form that distinguishes it."""
    if as_of.tzinfo is None:
        raise EdgarPayloadError("filer-regime cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    observations: list[tuple[datetime, str, bool]] = []
    for filing in filings:
        if filing.accepted > cutoff:
            continue
        form = filing.form.upper().removesuffix("/A")
        if form in _DOMESTIC_FORMS:
            observations.append((filing.accepted, filing.accession, True))
        elif form in _FOREIGN_FORMS:
            observations.append((filing.accepted, filing.accession, False))
    if not observations:
        return None
    return max(observations)[2]


def _merge_form_observations(
    observations: tuple[SubmissionFormObservation, ...],
) -> tuple[SubmissionFormObservation, ...]:
    rows: dict[str, SubmissionFormObservation] = {}
    for observation in observations:
        prior = rows.get(observation.accession)
        if prior is not None and (
            prior.cik,
            prior.accession,
            prior.form,
            prior.accepted,
        ) != (
            observation.cik,
            observation.accession,
            observation.form,
            observation.accepted,
        ):
            raise EdgarPayloadError(
                f"conflicting submissions form history for {observation.accession}"
            )
        if prior is None or (observation.source_url, observation.source_sha256) < (
            prior.source_url,
            prior.source_sha256,
        ):
            rows[observation.accession] = observation
    return tuple(sorted(rows.values(), key=lambda row: (row.accepted, row.accession)))


def _target_pairs(plan: FilingDiscoveryPlan) -> dict[int, frozenset[tuple[str, str]]]:
    pairs: dict[int, set[tuple[str, str]]] = {}
    for row in plan.rows:
        if (
            row.status != "discovered"
            or len(row.candidate_ciks) != 1
            or row.normalized_exchange is None
        ):
            continue
        cik = row.candidate_ciks[0]
        pairs.setdefault(cik, set()).add(
            (normalize_ticker(row.ticker), normalize_exchange(row.normalized_exchange))
        )
    return {cik: frozenset(values) for cik, values in pairs.items()}


def _parsed_pairs(parsed: FilingParseResult) -> frozenset[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for cover in extract_cover_security_classes(parsed):
        try:
            pairs.add((normalize_ticker(cover.ticker), normalize_exchange(cover.exchange)))
        except SecurityMasterError:
            continue
    return frozenset(pairs)


def acquire_cover_evidence(
    client: EdgarClient,
    plan: FilingDiscoveryPlan,
    archive_root: str | Path,
    *,
    as_of: datetime,
    maximum_filings_per_cik: int = 4,
    maximum_ciks: int | None = None,
    start_after_cik: int | None = None,
    refresh: bool = False,
) -> CoverAcquisitionResult:
    """Archive and parse a deterministic CIK shard; immutable archives make reruns resumable."""
    if as_of.tzinfo is None:
        raise EdgarPayloadError("cover acquisition cutoff must be timezone-aware")
    if maximum_filings_per_cik <= 0 or (maximum_ciks is not None and maximum_ciks <= 0):
        raise EdgarPayloadError("cover acquisition limits must be positive")
    cutoff = as_of.astimezone(UTC)
    pairs_by_cik = _target_pairs(plan)
    eligible = tuple(
        cik for cik in sorted(pairs_by_cik) if start_after_cik is None or cik > start_after_cik
    )
    requested = eligible[:maximum_ciks] if maximum_ciks is not None else eligible
    deferred = eligible[len(requested) :]
    evidence: list[CoverFilingEvidence] = []
    archives: list[CoverArchiveRecord] = []
    share_observations: list[CoverShareObservation] = []
    fpi_form_observations: list[CoverFpiFormObservation] = []
    form_history_proofs: list[CoverFormHistoryProof] = []
    gaps: list[CoverAcquisitionGap] = []
    selected_count = 0
    archived_count = 0

    for cik in requested:
        submissions_document = client.submissions(cik, refresh=refresh)
        feed = parse_submissions_document(submissions_document)
        if feed.cik != cik:
            raise EdgarPayloadError("SEC submissions CIK does not match the discovery plan")
        if feed.unusable_filings:
            gaps.append(
                CoverAcquisitionGap(
                    cik,
                    None,
                    "unusable_submission_rows",
                    f"{feed.unusable_filings} submissions rows lack an archivable primary document",
                )
            )
        if feed.unusable_current_symbols:
            gaps.append(
                CoverAcquisitionGap(
                    cik,
                    None,
                    "unusable_current_symbol_rows",
                    (
                        f"{feed.unusable_current_symbols} current ticker/exchange rows "
                        "are incomplete and were quarantined"
                    ),
                )
            )
        form_rows = list(feed.form_history)
        source_documents = [
            f"{submissions_document.url}#{submissions_document.content_sha256}"
        ]
        form_history_complete = True
        for filename in feed.history_files:
            try:
                history_document = client.submission_history(filename, refresh=refresh)
            except EdgarHttpError as exc:
                if exc.status != 404:
                    raise
                form_history_complete = False
                gaps.append(
                    CoverAcquisitionGap(
                        cik,
                        None,
                        "submission_history_missing",
                        f"SEC returned HTTP 404 for declared submissions history {filename}",
                    )
                )
                continue
            form_rows.extend(
                parse_submission_history_forms(
                    history_document.payload,
                    cik=cik,
                    source_url=history_document.url,
                    source_sha256=history_document.content_sha256,
                )
            )
            source_documents.append(
                f"{history_document.url}#{history_document.content_sha256}"
            )
        form_history = _merge_form_observations(tuple(form_rows))
        for row in form_history:
            base_form = row.form.upper().removesuffix("/A")
            if row.accepted <= cutoff and base_form in _FPI_CLASSIFICATION_FORMS:
                fpi_form_observations.append(
                    CoverFpiFormObservation(
                        cik,
                        row.accession,
                        row.form,
                        row.accepted,
                        f"{row.source_url}#{row.source_sha256}:{row.accession}",
                    )
                )
        if form_history_complete:
            proof_sources = tuple(sorted(set(source_documents)))
            proof_payload = {
                "cik": cik,
                "as_of": cutoff.isoformat(),
                "declared_history_files": list(feed.history_files),
                "source_documents": list(proof_sources),
            }
            digest = hashlib.sha256(
                json.dumps(proof_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            form_history_proofs.append(
                CoverFormHistoryProof(
                    cik,
                    cutoff,
                    proof_sources,
                    f"sec-submissions-complete://{cik}/{digest}",
                )
            )
        domestic_flag = infer_domestic_flag(form_history, as_of=cutoff)
        if domestic_flag is None:
            gaps.append(
                CoverAcquisitionGap(
                    cik,
                    None,
                    "unknown_filer_regime",
                    "no accepted domestic-or-foreign distinguishing form exists at the cutoff",
                )
            )
            continue
        selected = select_cover_filings(
            feed,
            as_of=cutoff,
            maximum=maximum_filings_per_cik,
        )
        selected_count += len(selected)
        if not selected:
            gaps.append(
                CoverAcquisitionGap(
                    cik,
                    None,
                    "no_cover_filing",
                    "no cover-capable filing was accepted by the PIT cutoff",
                )
            )
            continue

        allowed_pairs = pairs_by_cik[cik]
        for filing in selected:
            try:
                archived = archive_filing(client, filing, archive_root, refresh=refresh)
            except EdgarHttpError as exc:
                if exc.status != 404:
                    raise
                gaps.append(
                    CoverAcquisitionGap(
                        cik,
                        filing.accession,
                        "filing_resource_missing",
                        "SEC returned HTTP 404 for a required filing archive resource",
                    )
                )
                continue
            archived_count += 1
            primary_name = PurePosixPath(filing.primary_document).name
            primary_resource = next(
                (resource for resource in archived.resources if resource.name == primary_name),
                None,
            )
            if primary_resource is None:
                raise EdgarPayloadError("archived filing primary resource is missing")
            archives.append(
                CoverArchiveRecord(
                    cik,
                    filing.accession,
                    archived.snapshot_id,
                    primary_resource.content_sha256,
                )
            )
            primary_path = archived.output_dir / primary_name
            if not primary_path.is_file():
                raise EdgarPayloadError("archived filing primary document is missing")
            try:
                parsed = parse_filing_xbrl(
                    primary_path.read_bytes(),
                    filing=filing,
                    source_document=primary_name,
                )
            except EdgarPayloadError as exc:
                gaps.append(
                    CoverAcquisitionGap(
                        cik,
                        filing.accession,
                        "primary_parse_failure",
                        str(exc),
                    )
                )
                continue
            if not (_parsed_pairs(parsed) & allowed_pairs):
                gaps.append(
                    CoverAcquisitionGap(
                        cik,
                        filing.accession,
                        "cover_not_in_discovery_plan",
                        "filing cover classes do not match a discovered ticker/exchange pair",
                    )
                )
                continue
            for cover in extract_cover_security_classes(parsed):
                try:
                    pair = (normalize_ticker(cover.ticker), normalize_exchange(cover.exchange))
                    security, _ = security_evidence_from_cover(
                        filing,
                        cover,
                        domestic_flag=domestic_flag,
                    )
                except SecurityMasterError:
                    continue
                if (
                    pair in allowed_pairs
                    and cover.shares_outstanding is not None
                    and cover.shares_evidence_pointer is not None
                ):
                    share_observations.append(
                        CoverShareObservation(
                            security.security_id,
                            cik,
                            filing.accepted,
                            cover.shares_outstanding,
                            cover.shares_evidence_pointer,
                        )
                    )
            evidence.append(
                CoverFilingEvidence(
                    filing,
                    parsed,
                    domestic_flag,
                    allowed_pairs,
                )
            )

    return CoverAcquisitionResult(
        plan.snapshot_id,
        cutoff,
        tuple(requested),
        tuple(deferred),
        start_after_cik,
        selected_count,
        archived_count,
        tuple(archives),
        tuple(share_observations),
        tuple(evidence),
        tuple(gaps),
        tuple(fpi_form_observations),
        tuple(form_history_proofs),
    )
