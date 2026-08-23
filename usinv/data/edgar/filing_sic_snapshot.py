"""Immutable filing-header SIC supplements for the Phase 2.3 sector gate."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from usinv.data.edgar.client import EdgarClient, EdgarHttpError, EdgarPayloadError
from usinv.data.edgar.cover_shards import CoverEvidenceSnapshot, read_cover_evidence_snapshot
from usinv.data.edgar.filing_header import parse_filing_header_metadata

FILING_SIC_SNAPSHOT_VERSION: Final = "usinv-filing-sic-snapshot-v2"
_SUPPORTED_SNAPSHOT_VERSIONS: Final = {
    "usinv-filing-sic-snapshot-v1",
    FILING_SIC_SNAPSHOT_VERSION,
}


def _publish_directory(temporary: Path, target: Path) -> None:
    for attempt in range(6):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if target.exists():
                return
            if attempt == 5:
                raise
            time.sleep(0.05 * (2**attempt))


@dataclass(frozen=True, slots=True)
class FilingSicRecord:
    cik: int
    accession: str
    accepted: datetime
    sic: int
    source_url: str
    source_sha256: str
    header_sha256: str
    source_kind: str = "header_sic"


@dataclass(frozen=True, slots=True)
class FilingSicSnapshot:
    snapshot_id: str
    output_dir: Path
    cover_snapshot_id: str
    as_of: datetime
    target_ciks: tuple[int, ...]
    records: tuple[FilingSicRecord, ...]
    gaps: tuple[int, ...]
    from_cache: bool


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _header_slice(body: bytes) -> bytes:
    marker = b"</SEC-HEADER>"
    end = body.upper().find(marker)
    if end >= 0:
        return body[: end + len(marker)]
    return body[: 256 * 1024]


def _record_payload(record: FilingSicRecord, header: bytes) -> dict[str, object]:
    return {
        "cik": record.cik,
        "accession": record.accession,
        "accepted": record.accepted.astimezone(UTC).isoformat(),
        "sic": record.sic,
        "source_url": record.source_url,
        "source_sha256": record.source_sha256,
        "header_sha256": record.header_sha256,
        "source_kind": record.source_kind,
        "header_base64": base64.b64encode(header).decode("ascii"),
    }


def _read_payload(path: Path) -> tuple[dict[str, object], str]:
    try:
        payload = json.loads((path / "filing-sic.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("filing-SIC snapshot is unreadable") from exc
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    if path.name != snapshot_id or payload.get("version") not in _SUPPORTED_SNAPSHOT_VERSIONS:
        raise EdgarPayloadError("filing-SIC snapshot identity is invalid")
    return payload, snapshot_id


def read_filing_sic_snapshot(path: str | Path) -> FilingSicSnapshot:
    """Read and reparse every archived header excerpt before returning evidence."""

    root = Path(path)
    payload, snapshot_id = _read_payload(root)
    try:
        as_of = datetime.fromisoformat(payload["as_of"])
        target_ciks = tuple(payload["target_ciks"])
        gaps = tuple(payload["gaps"])
        records: list[FilingSicRecord] = []
        for row in payload["records"]:
            header = base64.b64decode(row["header_base64"], validate=True)
            if hashlib.sha256(header).hexdigest() != row["header_sha256"]:
                raise ValueError("header hash")
            source_kind = row.get("source_kind", "header_sic")
            parsed = parse_filing_header_metadata(
                header,
                cik=row["cik"],
                allow_814_industry_fallback=source_kind == "sec_file_number_814",
            )
            accepted = datetime.fromisoformat(row["accepted"])
            if (
                parsed is None
                or parsed.accepted != accepted.astimezone(UTC)
                or parsed.sic != row["sic"]
                or parsed.source_kind != source_kind
            ):
                raise ValueError("header parse")
            records.append(
                FilingSicRecord(
                    row["cik"],
                    row["accession"],
                    accepted,
                    row["sic"],
                    row["source_url"],
                    row["source_sha256"],
                    row["header_sha256"],
                    source_kind,
                )
            )
        cover_snapshot_id = payload["cover_snapshot_id"]
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise EdgarPayloadError("filing-SIC snapshot payload is invalid") from exc
    if (
        as_of.tzinfo is None
        or len(cover_snapshot_id) != 64
        or tuple(sorted(set(target_ciks))) != target_ciks
        or tuple(sorted(set(gaps))) != gaps
        or not set(gaps) <= set(target_ciks)
        or any(row.cik not in target_ciks for row in records)
        or any(row.accepted.astimezone(UTC) > as_of.astimezone(UTC) for row in records)
        or len({row.cik for row in records}) != len(records)
    ):
        raise EdgarPayloadError("filing-SIC snapshot provenance is invalid")
    return FilingSicSnapshot(
        snapshot_id,
        root,
        cover_snapshot_id,
        as_of,
        target_ciks,
        tuple(sorted(records, key=lambda row: row.cik)),
        gaps,
        True,
    )


def rebase_filing_sic_snapshot(
    source: FilingSicSnapshot,
    old_cover: CoverEvidenceSnapshot,
    new_cover: CoverEvidenceSnapshot,
    output_root: str | Path,
) -> FilingSicSnapshot:
    """Rebind unchanged archived SIC evidence to a parser-only cover replacement."""

    verified_source = read_filing_sic_snapshot(source.output_dir)
    verified_old = read_cover_evidence_snapshot(old_cover.output_dir)
    verified_new = read_cover_evidence_snapshot(new_cover.output_dir)
    targets = set(verified_source.target_ciks)

    def target_archives(cover: CoverEvidenceSnapshot) -> tuple[tuple[int, str], ...]:
        return tuple(
            sorted((row.cik, row.accession) for row in cover.merge.archives if row.cik in targets)
        )

    if (
        verified_source.snapshot_id != source.snapshot_id
        or verified_old.snapshot_id != old_cover.snapshot_id
        or verified_new.snapshot_id != new_cover.snapshot_id
        or verified_source.cover_snapshot_id != verified_old.snapshot_id
        or verified_old.merge.as_of.astimezone(UTC) != verified_new.merge.as_of.astimezone(UTC)
        or verified_source.as_of.astimezone(UTC) != verified_new.merge.as_of.astimezone(UTC)
        or not targets <= set(verified_old.merge.requested_ciks)
        or not targets <= set(verified_new.merge.requested_ciks)
        or target_archives(verified_old) != target_archives(verified_new)
    ):
        raise EdgarPayloadError("filing-SIC rebase inputs do not preserve exact provenance")

    source_payload, source_snapshot_id = _read_payload(verified_source.output_dir)
    if source_snapshot_id != verified_source.snapshot_id:
        raise EdgarPayloadError("filing-SIC rebase source identity changed")
    payload = {**source_payload, "cover_snapshot_id": verified_new.snapshot_id}
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    root = (
        Path(output_root) / "filing-sic" / verified_source.as_of.astimezone(UTC).date().isoformat()
    )
    target = root / snapshot_id
    if target.exists():
        return read_filing_sic_snapshot(target)
    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        (temporary / "filing-sic.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        _publish_directory(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    created = read_filing_sic_snapshot(target)
    return FilingSicSnapshot(
        created.snapshot_id,
        created.output_dir,
        created.cover_snapshot_id,
        created.as_of,
        created.target_ciks,
        created.records,
        created.gaps,
        False,
    )


def acquire_filing_sic_snapshot(
    client: EdgarClient,
    cover: CoverEvidenceSnapshot,
    target_ciks: tuple[int, ...],
    output_root: str | Path,
    *,
    refresh: bool = False,
) -> FilingSicSnapshot:
    """Fetch archived complete-submission headers and retain the latest PIT SIC."""

    verified = read_cover_evidence_snapshot(cover.output_dir)
    targets = tuple(sorted(set(target_ciks)))
    if (
        verified.snapshot_id != cover.snapshot_id
        or not targets
        or targets != target_ciks
        or not set(targets) <= set(verified.merge.requested_ciks)
    ):
        raise EdgarPayloadError("filing-SIC acquisition targets are invalid")
    archives: dict[int, set[str]] = {}
    for row in verified.merge.archives:
        if row.cik in targets:
            archives.setdefault(row.cik, set()).add(row.accession)

    cutoff = verified.merge.as_of.astimezone(UTC)

    def acquire_one(cik: int) -> tuple[FilingSicRecord, bytes] | None:
        candidates: list[tuple[FilingSicRecord, bytes]] = []
        for accession in sorted(archives.get(cik, ()), reverse=True):
            try:
                resource = client.filing_resource(
                    cik,
                    accession,
                    f"{accession}.txt",
                    refresh=refresh,
                )
            except EdgarHttpError as exc:
                if exc.status == 404:
                    continue
                raise
            header = _header_slice(resource.body)
            metadata = parse_filing_header_metadata(
                header,
                cik=cik,
                allow_814_industry_fallback=True,
            )
            if metadata is None or metadata.accepted > cutoff:
                continue
            candidates.append(
                (
                    FilingSicRecord(
                        cik,
                        accession,
                        metadata.accepted,
                        metadata.sic,
                        resource.url,
                        resource.content_sha256,
                        hashlib.sha256(header).hexdigest(),
                        metadata.source_kind,
                    ),
                    header,
                )
            )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0].accepted, item[0].accession))

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
        acquired = tuple(executor.map(acquire_one, targets))
    selected = [item for item in acquired if item is not None]
    gaps = [cik for cik, item in zip(targets, acquired, strict=True) if item is None]

    payload = {
        "version": FILING_SIC_SNAPSHOT_VERSION,
        "cover_snapshot_id": verified.snapshot_id,
        "as_of": cutoff.isoformat(),
        "target_ciks": list(targets),
        "records": [_record_payload(record, header) for record, header in selected],
        "gaps": sorted(gaps),
    }
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    root = Path(output_root) / "filing-sic" / cutoff.date().isoformat()
    target = root / snapshot_id
    if target.exists():
        return read_filing_sic_snapshot(target)
    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        (temporary / "filing-sic.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        _publish_directory(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    created = read_filing_sic_snapshot(target)
    return FilingSicSnapshot(
        created.snapshot_id,
        created.output_dir,
        created.cover_snapshot_id,
        created.as_of,
        created.target_ciks,
        created.records,
        created.gaps,
        False,
    )
