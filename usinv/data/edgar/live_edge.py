"""Materialize as-filed live-edge facts for the existing PIT builder."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.data.edgar.client import EdgarClient, EdgarPayloadError
from usinv.data.edgar.companyfacts import CompanyFactsResult, compare_edge_to_fsds
from usinv.data.edgar.filing_header import parse_filing_sic
from usinv.data.edgar.filing_xbrl import (
    FILING_XBRL_VERSION,
    FilingArchiveResult,
    FilingFact,
    FilingParseResult,
    archive_filing,
    consolidated_filing_facts,
    extract_cover_security_classes,
    filing_facts_to_raw,
    parse_filing_xbrl,
    parse_presentation_linkbase,
)
from usinv.data.edgar.fsds import FACTS_RAW_SCHEMA
from usinv.data.edgar.pit_store import PitInputBatch
from usinv.data.edgar.submissions import SubmissionFiling
from usinv.data.edgar.tag_chains import PresentationRow

LIVE_EDGE_VERSION: Final = "usinv-live-edge-v2"
_FOUR_PLACES: Final = Decimal("0.0001")

EDGE_FACTS_SCHEMA: Final = pa.schema(
    [
        ("cik", pa.int64()),
        ("accession", pa.string()),
        ("tag", pa.string()),
        ("taxonomy", pa.string()),
        ("custom", pa.bool_()),
        ("context_id", pa.string()),
        ("period_start", pa.date32()),
        ("period_end", pa.date32()),
        ("ddate", pa.date32()),
        ("qtrs", pa.int16()),
        ("unit", pa.string()),
        ("decimals", pa.string()),
        ("value_text", pa.string()),
        ("text_value", pa.string()),
        ("dimensions_json", pa.string()),
        ("accepted", pa.timestamp("us", tz="UTC")),
        ("form", pa.string()),
        ("filed", pa.date32()),
        ("filing_period", pa.date32()),
        ("source_document", pa.string()),
        ("evidence_pointer", pa.string()),
    ],
    metadata={b"usinv_table": b"filing_facts", b"schema_version": b"1"},
)
EDGE_PRE_SCHEMA: Final = pa.schema(
    [
        ("adsh", pa.string()),
        ("report", pa.string()),
        ("line", pa.string()),
        ("stmt", pa.string()),
        ("tag", pa.string()),
        ("version", pa.string()),
        ("plabel", pa.string()),
    ],
    metadata={b"usinv_table": b"filing_presentation", b"schema_version": b"1"},
)


@dataclass(frozen=True, slots=True)
class LiveEdgeIssue:
    kind: str
    tag: str | None
    context_id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class LiveEdgeSnapshot:
    snapshot_id: str
    batch_id: str
    source_quarter: str
    source_sha256: str
    output_dir: Path
    facts_raw_rows: int
    filing_fact_rows: int
    presentation_rows: int
    issues: tuple[LiveEdgeIssue, ...]
    from_cache: bool
    cik: int | None = None
    accession: str | None = None
    accepted: datetime | None = None
    filing_sic: int | None = None
    filing_sic_evidence_pointer: str | None = None

    def pit_input(self) -> PitInputBatch:
        path = self.output_dir / "facts_raw.parquet"
        digest, byte_count = _sha256_file(path)
        parquet = pq.ParquetFile(path)
        return PitInputBatch(
            batch_id=self.batch_id,
            source_quarter=self.source_quarter,
            source_sha256=self.source_sha256,
            facts_path=path,
            facts_content_sha256=digest,
            byte_count=byte_count,
            row_count=parquet.metadata.num_rows,
            arrow_schema=str(parquet.schema_arrow),
        )


@dataclass(frozen=True, slots=True)
class LiveEdgeIngestResult:
    snapshot: LiveEdgeSnapshot
    archive: FilingArchiveResult
    parsed_facts: int
    cover_security_classes: int
    companyfacts_overlap: int
    companyfacts_mismatches: int


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _quarter(value: datetime) -> str:
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year}q{quarter}"


def _edge_rows(facts: tuple[FilingFact, ...]) -> list[dict[str, object]]:
    return [
        {
            "cik": fact.cik,
            "accession": fact.accession,
            "tag": fact.tag,
            "taxonomy": fact.taxonomy,
            "custom": fact.custom,
            "context_id": fact.context_id,
            "period_start": fact.period_start,
            "period_end": fact.period_end,
            "ddate": fact.ddate,
            "qtrs": fact.qtrs,
            "unit": fact.unit,
            "decimals": fact.decimals,
            "value_text": str(fact.value) if fact.value is not None else None,
            "text_value": fact.text_value,
            "dimensions_json": (
                json.dumps(fact.dimensions, separators=(",", ":")) if fact.dimensions else None
            ),
            "accepted": fact.accepted.astimezone(UTC),
            "form": fact.form,
            "filed": fact.filed,
            "filing_period": fact.filing_period,
            "source_document": fact.source_document,
            "evidence_pointer": fact.evidence_pointer,
        }
        for fact in facts
    ]


def _raw_rows(
    parsed: FilingParseResult,
    *,
    batch_id: str,
    source_quarter: str,
    source_sha256: str,
) -> tuple[list[dict[str, object]], tuple[LiveEdgeIssue, ...]]:
    rows: list[dict[str, object]] = []
    issues: list[LiveEdgeIssue] = []
    consolidated, conflicts = consolidated_filing_facts(parsed)
    for fact in consolidated:
        try:
            with localcontext() as context:
                context.prec = max(50, len(fact.value.as_tuple().digits) + 25)
                quantized = fact.value.quantize(_FOUR_PLACES)
            integer_digits = max(fact.value.adjusted() + 1, 0) if fact.value else 1
        except InvalidOperation:
            quantized = None
            integer_digits = 25
        if quantized != fact.value or integer_digits > 24:
            issues.append(
                LiveEdgeIssue(
                    "precision_exceeds_fsds_decimal_contract",
                    fact.tag,
                    fact.context_id,
                    f"value {fact.value} cannot enter Decimal128(28,4) losslessly",
                )
            )
            continue
        row = {
            "batch_id": batch_id,
            "source_quarter": source_quarter,
            "source_sha256": source_sha256,
            "adsh": fact.accession,
            "cik": fact.cik,
            "form": fact.form,
            "filing_period": fact.filing_period,
            "filing_fy": None,
            "filing_fp": None,
            "filed": fact.filed,
            "accepted": fact.accepted.astimezone(UTC),
            "filing_prevrpt": None,
            "tag": fact.tag,
            "version": fact.accession if fact.custom else fact.taxonomy,
            "ddate": fact.ddate,
            "qtrs": fact.qtrs,
            "uom": fact.unit,
            "segments": None,
            "coreg": None,
            "value": quantized,
            "footnote": None,
            "is_consolidated": True,
        }
        rows.append(row)
    issues.extend(
        LiveEdgeIssue(item.kind, item.tag, item.context_id, item.detail) for item in conflicts
    )
    return rows, tuple(issues)


def _presentation_rows(rows: tuple[PresentationRow, ...]) -> list[dict[str, object]]:
    return [
        {
            "adsh": item.adsh,
            "report": str(item.report),
            "line": str(item.line),
            "stmt": item.stmt,
            "tag": item.tag,
            "version": item.version,
            "plabel": item.plabel,
        }
        for item in rows
    ]


def _verify(path: Path, snapshot_id: str) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("live-edge snapshot manifest is unreadable") from exc
    if manifest.get("snapshot_id") != snapshot_id:
        raise ValueError("live-edge snapshot identity mismatch")
    for name, schema in (
        ("facts_raw", FACTS_RAW_SCHEMA),
        ("filing_facts", EDGE_FACTS_SCHEMA),
        ("presentation", EDGE_PRE_SCHEMA),
    ):
        item = manifest.get("artifacts", {}).get(name, {})
        artifact_path = path / f"{name}.parquet"
        digest, _ = _sha256_file(artifact_path)
        if digest != item.get("sha256") or not pq.ParquetFile(artifact_path).schema_arrow.equals(
            schema, check_metadata=True
        ):
            raise ValueError(f"live-edge artifact failed verification: {name}")


def read_live_edge_snapshot(path: str | Path) -> LiveEdgeSnapshot:
    """Read one verified live-edge batch for composition into a PIT store."""

    root = Path(path)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        schema_version = int(manifest["schema_version"])
        live_edge_version = str(manifest["live_edge_version"])
        snapshot_id = str(manifest["snapshot_id"])
        batch_id = str(manifest["batch_id"])
        source_quarter = str(manifest["source_quarter"])
        source_sha256 = str(manifest["source_sha256"])
        cik = int(manifest["cik"])
        accession = str(manifest["accession"])
        accepted = datetime.fromisoformat(str(manifest["accepted"]))
        if accepted.tzinfo is None:
            raise ValueError("accepted")
        accepted = accepted.astimezone(UTC)
        filing_sic = int(manifest["filing_sic"]) if manifest["filing_sic"] is not None else None
        filing_sic_pointer = manifest["filing_sic_evidence_pointer"]
        artifacts = manifest["artifacts"]
        issues = tuple(
            LiveEdgeIssue(
                str(item["kind"]),
                str(item["tag"]) if item["tag"] is not None else None,
                str(item["context_id"]) if item["context_id"] is not None else None,
                str(item["detail"]),
            )
            for item in manifest["issues"]
        )
        facts_raw_rows = int(artifacts["facts_raw"]["rows"])
        filing_fact_rows = int(artifacts["filing_facts"]["rows"])
        presentation_rows = int(artifacts["presentation"]["rows"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("live-edge snapshot manifest is invalid") from exc
    if (
        root.name != snapshot_id
        or schema_version != 1
        or live_edge_version != LIVE_EDGE_VERSION
        or len(snapshot_id) != 64
        or not batch_id
        or not source_quarter
        or len(source_sha256) != 64
        or cik <= 0
        or not accession
        or accepted.tzinfo is None
        or (filing_sic is not None and not 100 <= filing_sic <= 9999)
        or (
            filing_sic is not None
            and (not isinstance(filing_sic_pointer, str) or not filing_sic_pointer.strip())
        )
        or (filing_sic is None and filing_sic_pointer is not None)
        or min(facts_raw_rows, filing_fact_rows, presentation_rows) < 0
    ):
        raise EdgarPayloadError("live-edge snapshot provenance is invalid")
    try:
        _verify(root, snapshot_id)
    except ValueError as exc:
        raise EdgarPayloadError("live-edge snapshot verification failed") from exc
    return LiveEdgeSnapshot(
        snapshot_id,
        batch_id,
        source_quarter,
        source_sha256,
        root,
        facts_raw_rows,
        filing_fact_rows,
        presentation_rows,
        issues,
        True,
        cik,
        accession,
        accepted,
        filing_sic,
        str(filing_sic_pointer) if filing_sic_pointer is not None else None,
    )


def materialize_live_edge(
    filing: SubmissionFiling,
    archive: FilingArchiveResult,
    parsed: FilingParseResult,
    presentation: tuple[PresentationRow, ...],
    output_root: str | Path,
    *,
    filing_sic: int | None = None,
    filing_sic_evidence_pointer: str | None = None,
) -> LiveEdgeSnapshot:
    """Write a content-addressed filing batch consumable by the Phase 1.3 PIT store."""
    if (
        (filing_sic is not None and not 100 <= filing_sic <= 9999)
        or (filing_sic is not None and not filing_sic_evidence_pointer)
        or (filing_sic is None and filing_sic_evidence_pointer is not None)
    ):
        raise EdgarPayloadError("live-edge filing SIC provenance is invalid")
    source_quarter = _quarter(filing.accepted)
    source_sha256 = archive.snapshot_id
    batch_id = f"sec-live:{filing.accession}:{archive.snapshot_id[:16]}"
    descriptor = {
        "version": LIVE_EDGE_VERSION,
        "parser_version": FILING_XBRL_VERSION,
        "batch_id": batch_id,
        "source_quarter": source_quarter,
        "source_sha256": source_sha256,
        "cik": filing.cik,
        "accession": filing.accession,
        "accepted": filing.accepted.astimezone(UTC).isoformat(),
        "filing_sic": filing_sic,
        "filing_sic_evidence_pointer": filing_sic_evidence_pointer,
        "facts": [
            (fact.tag, fact.context_id, fact.decimals, str(fact.value), fact.text_value)
            for fact in parsed.facts
        ],
        "presentation": [
            (row.report, row.line, row.stmt, row.tag, row.version, row.plabel)
            for row in presentation
        ],
    }
    snapshot_id = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    root = Path(output_root) / "snapshots"
    target = root / snapshot_id
    raw_rows, normalization_issues = _raw_rows(
        parsed,
        batch_id=batch_id,
        source_quarter=source_quarter,
        source_sha256=source_sha256,
    )
    issues = (
        tuple(
            LiveEdgeIssue(item.kind, item.tag, item.context_id, item.detail)
            for item in parsed.issues
        )
        + normalization_issues
    )
    if target.exists():
        _verify(target, snapshot_id)
        return LiveEdgeSnapshot(
            snapshot_id,
            batch_id,
            source_quarter,
            source_sha256,
            target,
            len(raw_rows),
            len(parsed.facts),
            len(presentation),
            issues,
            True,
            filing.cik,
            filing.accession,
            filing.accepted.astimezone(UTC),
            filing_sic,
            filing_sic_evidence_pointer,
        )

    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        pq.write_table(
            pa.Table.from_pylist(raw_rows, schema=FACTS_RAW_SCHEMA),
            temporary / "facts_raw.parquet",
        )
        pq.write_table(
            pa.Table.from_pylist(_edge_rows(parsed.facts), schema=EDGE_FACTS_SCHEMA),
            temporary / "filing_facts.parquet",
        )
        pq.write_table(
            pa.Table.from_pylist(_presentation_rows(presentation), schema=EDGE_PRE_SCHEMA),
            temporary / "presentation.parquet",
        )
        artifacts = {}
        for name in ("facts_raw", "filing_facts", "presentation"):
            path = temporary / f"{name}.parquet"
            digest, byte_count = _sha256_file(path)
            artifacts[name] = {
                "sha256": digest,
                "bytes": byte_count,
                "rows": pq.ParquetFile(path).metadata.num_rows,
            }
        manifest = {
            "schema_version": 1,
            "live_edge_version": LIVE_EDGE_VERSION,
            "snapshot_id": snapshot_id,
            "batch_id": batch_id,
            "source_quarter": source_quarter,
            "source_sha256": source_sha256,
            "cik": filing.cik,
            "accession": filing.accession,
            "accepted": filing.accepted.astimezone(UTC).isoformat(),
            "filing_sic": filing_sic,
            "filing_sic_evidence_pointer": filing_sic_evidence_pointer,
            "artifacts": artifacts,
            "issues": [
                {
                    "kind": issue.kind,
                    "tag": issue.tag,
                    "context_id": issue.context_id,
                    "detail": issue.detail,
                }
                for issue in issues
            ],
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _verify(target, snapshot_id)
    return LiveEdgeSnapshot(
        snapshot_id,
        batch_id,
        source_quarter,
        source_sha256,
        target,
        len(raw_rows),
        len(parsed.facts),
        len(presentation),
        issues,
        False,
        filing.cik,
        filing.accession,
        filing.accepted.astimezone(UTC),
        filing_sic,
        filing_sic_evidence_pointer,
    )


def ingest_periodic_filing(
    client: EdgarClient,
    filing: SubmissionFiling,
    companyfacts: CompanyFactsResult,
    *,
    archive_root: str | Path,
    output_root: str | Path,
    refresh: bool = False,
) -> LiveEdgeIngestResult:
    """Archive, parse, cross-check and materialize one accepted periodic filing."""
    archive = archive_filing(
        client,
        filing,
        archive_root,
        refresh=refresh,
        include_presentation=True,
        include_filing_header=True,
    )
    primary_name = Path(filing.primary_document).name
    candidate_names = (
        primary_name,
        *(
            item.name
            for item in archive.resources
            if item.name != primary_name
            and item.name.casefold().endswith(".xml")
            and not item.name.casefold().endswith(("_pre.xml", "_lab.xml"))
            and item.name.casefold() != "filingsummary.xml"
        ),
    )
    parse_errors: list[str] = []
    parsed: FilingParseResult | None = None
    for candidate_name in candidate_names:
        try:
            candidate = parse_filing_xbrl(
                (archive.output_dir / candidate_name).read_bytes(),
                filing=filing,
                source_document=candidate_name,
            )
        except EdgarPayloadError as exc:
            parse_errors.append(f"{candidate_name}: {exc}")
            continue
        if candidate.facts:
            parsed = candidate
            break
    if parsed is None:
        detail = "; ".join(parse_errors) if parse_errors else "no facts in archived documents"
        raise EdgarPayloadError(f"live-edge filing has no usable XBRL facts: {detail}")
    resource_names = {item.name for item in archive.resources}
    presentation_name = next(
        (name for name in sorted(resource_names) if name.casefold().endswith("_pre.xml")),
        None,
    )
    label_name = next(
        (name for name in sorted(resource_names) if name.casefold().endswith("_lab.xml")),
        None,
    )
    presentation = (
        parse_presentation_linkbase(
            (archive.output_dir / presentation_name).read_bytes(),
            accession=filing.accession,
            label_body=(archive.output_dir / label_name).read_bytes() if label_name else None,
        )
        if presentation_name
        else ()
    )
    complete_submission_name = f"{filing.accession}.txt".casefold()
    header_resource = next(
        (
            item
            for item in archive.resources
            if item.name.casefold() == complete_submission_name
        ),
        None,
    )
    filing_sic = (
        parse_filing_sic((archive.output_dir / header_resource.name).read_bytes())
        if header_resource is not None
        else None
    )
    filing_sic_pointer = (
        f"{header_resource.url}#standard-industrial-classification"
        f";sha256={header_resource.content_sha256}"
        if filing_sic is not None and header_resource is not None
        else None
    )
    snapshot = materialize_live_edge(
        filing,
        archive,
        parsed,
        presentation,
        output_root,
        filing_sic=filing_sic,
        filing_sic_evidence_pointer=filing_sic_pointer,
    )

    edge = filing_facts_to_raw(parsed)
    crosscheck = tuple(item for item in companyfacts.facts if item.adsh == filing.accession)
    edge_keys = {(item.cik, item.tag, item.ddate, item.qtrs, item.uom) for item in edge}
    crosscheck_keys = {(item.cik, item.tag, item.ddate, item.qtrs, item.uom) for item in crosscheck}
    parity = compare_edge_to_fsds(
        tuple(
            item
            for item in edge
            if (item.cik, item.tag, item.ddate, item.qtrs, item.uom) in crosscheck_keys
        ),
        tuple(
            item
            for item in crosscheck
            if (item.cik, item.tag, item.ddate, item.qtrs, item.uom) in edge_keys
        ),
    )
    return LiveEdgeIngestResult(
        snapshot,
        archive,
        len(parsed.facts),
        len(extract_cover_security_classes(parsed)),
        parity.compared_keys,
        len(parity.mismatches),
    )
