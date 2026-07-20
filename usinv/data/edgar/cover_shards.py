"""Compact immutable evidence shards and exact-set merging for cover bootstraps."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverAcquisitionResult,
    CoverArchiveRecord,
    CoverShareObservation,
)
from usinv.data.edgar.securities import (
    SecurityMaster,
    build_security_master,
    materialize_security_master,
    read_security_master_snapshot,
)
from usinv.data.edgar.security_bootstrap import (
    CoverBootstrapGap,
    CoverSecurityBootstrap,
)

COVER_SHARD_VERSION: Final = "usinv-cover-evidence-shard-v2"


@dataclass(frozen=True, slots=True)
class CoverEvidenceShard:
    snapshot_id: str
    plan_snapshot_id: str
    as_of: datetime
    requested_ciks: tuple[int, ...]
    start_after_cik: int | None
    deferred_ciks: int
    selected_filings: int
    archives: tuple[CoverArchiveRecord, ...]
    share_observations: tuple[CoverShareObservation, ...]
    acquisition_gaps: tuple[CoverAcquisitionGap, ...]
    bootstrap_gaps: tuple[CoverBootstrapGap, ...]
    master: SecurityMaster
    output_dir: Path


@dataclass(frozen=True, slots=True)
class CoverEvidenceMerge:
    plan_snapshot_id: str
    as_of: datetime
    requested_ciks: tuple[int, ...]
    shard_snapshot_ids: tuple[str, ...]
    archives: tuple[CoverArchiveRecord, ...]
    share_observations: tuple[CoverShareObservation, ...]
    acquisition_gaps: tuple[CoverAcquisitionGap, ...]
    bootstrap_gaps: tuple[CoverBootstrapGap, ...]
    master: SecurityMaster


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _payload(
    acquisition: CoverAcquisitionResult,
    bootstrap: CoverSecurityBootstrap,
    master_snapshot_id: str,
) -> dict[str, object]:
    return {
        "version": COVER_SHARD_VERSION,
        "plan_snapshot_id": acquisition.plan_snapshot_id,
        "as_of": acquisition.as_of.astimezone(UTC).isoformat(),
        "requested_ciks": list(acquisition.requested_ciks),
        "start_after_cik": acquisition.start_after_cik,
        "deferred_ciks": len(acquisition.deferred_ciks),
        "selected_filings": acquisition.selected_filings,
        "archived_filings": acquisition.archived_filings,
        "evidence_filings": len(acquisition.evidence),
        "archives": [asdict(row) for row in acquisition.archives],
        "share_observations": [
            {
                "security_id": row.security_id,
                "cik": row.cik,
                "accepted": row.accepted.astimezone(UTC).isoformat(),
                "shares_outstanding": str(row.shares_outstanding),
                "evidence_pointer": row.evidence_pointer,
            }
            for row in acquisition.share_observations
        ],
        "acquisition_gaps": [asdict(row) for row in acquisition.gaps],
        "bootstrap_gaps": [asdict(row) for row in bootstrap.gaps],
        "master_snapshot_id": master_snapshot_id,
    }


def _read_payload(path: Path) -> tuple[dict[str, object], str]:
    try:
        payload = json.loads((path / "shard.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("cover evidence shard metadata is unreadable") from exc
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    if path.name != snapshot_id or payload.get("version") != COVER_SHARD_VERSION:
        raise EdgarPayloadError("cover evidence shard identity is invalid")
    return payload, snapshot_id


def read_cover_evidence_shard(path: str | Path) -> CoverEvidenceShard:
    """Verify and open one compact derived shard; raw SEC resources can be refetched by hash."""
    root = Path(path)
    payload, snapshot_id = _read_payload(root)
    try:
        master_snapshot_id = payload["master_snapshot_id"]
        master = read_security_master_snapshot(root / "master" / "snapshots" / master_snapshot_id)
        archives = tuple(CoverArchiveRecord(**row) for row in payload["archives"])
        share_observations = tuple(
            CoverShareObservation(
                row["security_id"],
                row["cik"],
                datetime.fromisoformat(row["accepted"]),
                Decimal(row["shares_outstanding"]),
                row["evidence_pointer"],
            )
            for row in payload["share_observations"]
        )
        acquisition_gaps = tuple(CoverAcquisitionGap(**row) for row in payload["acquisition_gaps"])
        bootstrap_gaps = tuple(CoverBootstrapGap(**row) for row in payload["bootstrap_gaps"])
        as_of = datetime.fromisoformat(payload["as_of"])
        requested_ciks = tuple(payload["requested_ciks"])
        plan_snapshot_id = payload["plan_snapshot_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EdgarPayloadError("cover evidence shard metadata is invalid") from exc
    if (
        not isinstance(master_snapshot_id, str)
        or not isinstance(plan_snapshot_id, str)
        or len(plan_snapshot_id) != 64
        or as_of.tzinfo is None
        or tuple(sorted(set(requested_ciks))) != requested_ciks
        or any(not isinstance(cik, int) or cik <= 0 for cik in requested_ciks)
        or not isinstance(payload["deferred_ciks"], int)
        or payload["deferred_ciks"] < 0
        or not isinstance(payload["selected_filings"], int)
        or payload["selected_filings"] < 0
        or payload.get("archived_filings") != len(archives)
        or any(
            row.cik <= 0
            or row.cik not in requested_ciks
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", row.accession)
            or len(row.archive_snapshot_id) != 64
            or len(row.primary_sha256) != 64
            for row in archives
        )
        or any(row.cik not in requested_ciks for row in master.securities)
        or any(
            row.cik not in requested_ciks
            or row.security_id not in {security.security_id for security in master.securities}
            or row.accepted.astimezone(UTC) > as_of.astimezone(UTC)
            for row in share_observations
        )
        or any(row.cik not in requested_ciks for row in acquisition_gaps)
        or any(row.cik not in requested_ciks for row in bootstrap_gaps)
    ):
        raise EdgarPayloadError("cover evidence shard provenance is invalid")
    return CoverEvidenceShard(
        snapshot_id,
        plan_snapshot_id,
        as_of.astimezone(UTC),
        requested_ciks,
        payload["start_after_cik"],
        payload["deferred_ciks"],
        payload["selected_filings"],
        archives,
        share_observations,
        acquisition_gaps,
        bootstrap_gaps,
        master,
        root,
    )


def materialize_cover_evidence_shard(
    acquisition: CoverAcquisitionResult,
    bootstrap: CoverSecurityBootstrap,
    output_root: str | Path,
) -> CoverEvidenceShard:
    """Persist a shard-specific partial master under an explicit non-final evidence label."""
    if not acquisition.requested_ciks:
        raise EdgarPayloadError("an empty CIK shard cannot be materialized")
    if bootstrap.filings != len(acquisition.evidence):
        raise EdgarPayloadError("cover bootstrap does not match its acquisition evidence")
    requested = frozenset(acquisition.requested_ciks)
    if (
        acquisition.archived_filings != len(acquisition.archives)
        or any(row.cik not in requested for row in acquisition.archives)
        or any(row.cik not in requested for row in acquisition.share_observations)
        or any(row.cik not in requested for row in bootstrap.master.securities)
    ):
        raise EdgarPayloadError("cover evidence shard contains out-of-scope provenance")
    root = (
        Path(output_root)
        / "security-bootstrap"
        / "evidence-shards"
        / acquisition.plan_snapshot_id
        / acquisition.as_of.date().isoformat()
    )
    temporary = root / f".pending.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        master_snapshot = materialize_security_master(bootstrap.master, temporary / "master")
        payload = _payload(acquisition, bootstrap, master_snapshot.snapshot_id)
        snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
        target = root / snapshot_id
        if target.exists():
            shutil.rmtree(temporary, ignore_errors=True)
            existing = read_cover_evidence_shard(target)
            if _read_payload(existing.output_dir)[0] != payload:
                raise EdgarPayloadError("cover evidence shard cache conflicts with the input")
            return existing
        (temporary / "shard.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return read_cover_evidence_shard(target)


def merge_cover_evidence_shards(
    shards: Iterable[CoverEvidenceShard],
    *,
    expected_ciks: Iterable[int],
) -> CoverEvidenceMerge:
    """Merge only a complete, non-overlapping exact CIK partition for one plan and cutoff."""
    rows = tuple(sorted(shards, key=lambda row: row.snapshot_id))
    expected = tuple(sorted(set(expected_ciks)))
    if not rows or not expected or any(cik <= 0 for cik in expected):
        raise EdgarPayloadError("cover evidence merge requires non-empty valid inputs")
    plan_ids = {row.plan_snapshot_id for row in rows}
    cutoffs = {row.as_of.astimezone(UTC) for row in rows}
    if len(plan_ids) != 1 or len(cutoffs) != 1:
        raise EdgarPayloadError("cover evidence shards mix plans or PIT cutoffs")
    observed: set[int] = set()
    for row in rows:
        overlap = observed.intersection(row.requested_ciks)
        if overlap:
            raise EdgarPayloadError("cover evidence shards overlap CIK coverage")
        observed.update(row.requested_ciks)
    if tuple(sorted(observed)) != expected:
        raise EdgarPayloadError("cover evidence shards do not exactly cover expected CIKs")
    master = build_security_master(
        (security for row in rows for security in row.master.securities),
        (symbol for row in rows for symbol in row.master.symbols),
    )
    return CoverEvidenceMerge(
        rows[0].plan_snapshot_id,
        rows[0].as_of,
        expected,
        tuple(row.snapshot_id for row in rows),
        tuple(archive for row in rows for archive in row.archives),
        tuple(observation for row in rows for observation in row.share_observations),
        tuple(gap for row in rows for gap in row.acquisition_gaps),
        tuple(gap for row in rows for gap in row.bootstrap_gaps),
        master,
    )
