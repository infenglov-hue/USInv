"""Compact immutable evidence shards and exact-set merging for cover bootstraps."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from usinv.data.edgar.client import EdgarPayloadError
from usinv.data.edgar.cover_acquisition import (
    CoverAcquisitionGap,
    CoverAcquisitionResult,
    CoverArchiveRecord,
    CoverFormHistoryProof,
    CoverFpiFormObservation,
    CoverShareObservation,
    CoverTerminalFormObservation,
)
from usinv.data.edgar.securities import (
    Security,
    SecurityMaster,
    SymbolInterval,
    build_security_master,
    is_explicit_non_common_security_title,
    materialize_security_master,
    mint_security_id,
    read_security_master_snapshot,
)
from usinv.data.edgar.security_bootstrap import (
    CoverBootstrapGap,
    CoverSecurityBootstrap,
    FilingDiscoveryPlan,
    _collapse_symbol_observations,
)

COVER_IDENTITY_RECONCILIATION_VERSION: Final = "usinv-cover-semantic-equity-v2"
COVER_MERGE_VERSION: Final = "usinv-cover-evidence-merge-v5"
COVER_SHARD_VERSION: Final = "usinv-cover-evidence-shard-v5"
_SUPPORTED_COVER_MERGE_VERSIONS: Final = frozenset(
    {
        "usinv-cover-evidence-merge-v2",
        "usinv-cover-evidence-merge-v3",
        "usinv-cover-evidence-merge-v4",
        COVER_MERGE_VERSION,
    }
)
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_EQUITY_CLASS_PATTERN: Final = re.compile(
    r"\b(?:class|series)\s+([a-z0-9][a-z0-9-]*)\b",
    re.IGNORECASE,
)
_COMMON_EQUITY_PATTERN: Final = re.compile(
    r"\bcommon(?:\s+(?:stock|shares?))?\b",
    re.IGNORECASE,
)
_ORDINARY_EQUITY_PATTERN: Final = re.compile(r"\bordinary\s*shares?\b", re.IGNORECASE)
_DIMENSION_CLASS_PATTERNS: Final = (
    re.compile(r"commonclass([a-z0-9]+)$", re.IGNORECASE),
    re.compile(
        r"class([a-z0-9]+)(?:commonstock|commonshares?|ordinaryshares?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:commonstock|commonshares?|ordinaryshares?)class([a-z0-9]+)",
        re.IGNORECASE,
    ),
    re.compile(r"^class([a-z0-9]+)$", re.IGNORECASE),
)


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
    fpi_form_observations: tuple[CoverFpiFormObservation, ...]
    form_history_proofs: tuple[CoverFormHistoryProof, ...]
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
    fpi_form_observations: tuple[CoverFpiFormObservation, ...]
    form_history_proofs: tuple[CoverFormHistoryProof, ...]
    acquisition_gaps: tuple[CoverAcquisitionGap, ...]
    bootstrap_gaps: tuple[CoverBootstrapGap, ...]
    master: SecurityMaster


@dataclass(frozen=True, slots=True)
class CoverEvidenceSnapshot:
    snapshot_id: str
    output_dir: Path
    merge: CoverEvidenceMerge
    master_snapshot_id: str
    from_cache: bool


@dataclass(frozen=True, slots=True)
class CoverIdentityReconciliation:
    """One deterministic, evidence-preserving semantic identity rewrite."""

    merge: CoverEvidenceMerge
    collapsed_groups: int
    rewritten_security_ids: int
    ambiguous_groups: int


def patch_cover_evidence_merge(
    source: CoverEvidenceSnapshot,
    shard: CoverEvidenceShard,
) -> CoverEvidenceMerge:
    """Replace an exact CIK subset in complete evidence with a verified fresh shard."""

    targets = frozenset(shard.requested_ciks)
    if (
        not targets
        or shard.plan_snapshot_id != source.merge.plan_snapshot_id
        or shard.as_of.astimezone(UTC) != source.merge.as_of.astimezone(UTC)
        or not targets < frozenset(source.merge.requested_ciks)
        or shard.deferred_ciks != 0
    ):
        raise EdgarPayloadError("cover evidence patch does not match its complete source")
    return _replace_cover_subset(
        source,
        shard,
        plan_snapshot_id=source.merge.plan_snapshot_id,
        requested_ciks=source.merge.requested_ciks,
    )


def _discovery_pairs(
    plan: FilingDiscoveryPlan,
) -> dict[int, frozenset[tuple[str, str]]]:
    pairs: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for row in plan.rows:
        if (
            row.status in {"discovered", "ambiguous"}
            and row.candidate_ciks
            and row.normalized_exchange is not None
        ):
            for cik in row.candidate_ciks:
                pairs[cik].add((row.ticker, row.normalized_exchange))
    return {cik: frozenset(values) for cik, values in pairs.items()}


def cover_plan_changed_ciks(
    old_plan: FilingDiscoveryPlan,
    new_plan: FilingDiscoveryPlan,
) -> tuple[int, ...]:
    """Return only CIKs whose exact discovered ticker/exchange set changed."""

    old_pairs = _discovery_pairs(old_plan)
    new_pairs = _discovery_pairs(new_plan)
    return tuple(
        sorted(
            cik
            for cik in set(old_pairs) | set(new_pairs)
            if old_pairs.get(cik) != new_pairs.get(cik)
        )
    )


def rebase_cover_evidence_plan(
    source: CoverEvidenceSnapshot,
    old_plan: FilingDiscoveryPlan,
    new_plan: FilingDiscoveryPlan,
    shard: CoverEvidenceShard | None,
) -> CoverEvidenceMerge:
    """Carry unchanged cover evidence into a conservative discovery-plan extension."""

    old_pairs = _discovery_pairs(old_plan)
    new_pairs = _discovery_pairs(new_plan)
    changed = frozenset(cover_plan_changed_ciks(old_plan, new_plan))
    if (
        source.merge.plan_snapshot_id != old_plan.snapshot_id
        or tuple(sorted(old_pairs)) != source.merge.requested_ciks
        or tuple(sorted(new_pairs)) != tuple(sorted(set(source.merge.requested_ciks) | changed))
        or old_plan.listing_snapshot_id != new_plan.listing_snapshot_id
        or old_plan.listing_as_of != new_plan.listing_as_of
        or old_plan.association_source_sha256 != new_plan.association_source_sha256
        or old_plan.association_observed_at != new_plan.association_observed_at
    ):
        raise EdgarPayloadError("cover evidence plan rebase is not an exact conservative extension")
    if not changed:
        if shard is not None:
            raise EdgarPayloadError("metadata-only cover rebase must not provide a shard")
        return replace(source.merge, plan_snapshot_id=new_plan.snapshot_id)
    if (
        shard is None
        or shard.plan_snapshot_id != new_plan.snapshot_id
        or frozenset(shard.requested_ciks) != changed
        or shard.as_of.astimezone(UTC) != source.merge.as_of.astimezone(UTC)
        or shard.deferred_ciks != 0
    ):
        raise EdgarPayloadError("cover evidence plan rebase is not an exact conservative extension")
    return _replace_cover_subset(
        source,
        shard,
        plan_snapshot_id=new_plan.snapshot_id,
        requested_ciks=tuple(sorted(new_pairs)),
    )


def _replace_cover_subset(
    source: CoverEvidenceSnapshot,
    shard: CoverEvidenceShard,
    *,
    plan_snapshot_id: str,
    requested_ciks: tuple[int, ...],
) -> CoverEvidenceMerge:
    """Replace shard CIKs while retaining all unrelated immutable evidence."""

    targets = frozenset(shard.requested_ciks)
    retained_securities = tuple(
        row for row in source.merge.master.securities if row.cik not in targets
    )
    retained_security_ids = {row.security_id for row in retained_securities}
    master = build_security_master(
        (*retained_securities, *shard.master.securities),
        (
            *(
                row
                for row in source.merge.master.symbols
                if row.security_id in retained_security_ids
            ),
            *shard.master.symbols,
        ),
    )
    patched = replace(
        source.merge,
        plan_snapshot_id=plan_snapshot_id,
        requested_ciks=requested_ciks,
        shard_snapshot_ids=tuple(sorted({*source.merge.shard_snapshot_ids, shard.snapshot_id})),
        archives=tuple(row for row in source.merge.archives if row.cik not in targets)
        + shard.archives,
        share_observations=tuple(
            row for row in source.merge.share_observations if row.cik not in targets
        )
        + shard.share_observations,
        fpi_form_observations=tuple(
            row for row in source.merge.fpi_form_observations if row.cik not in targets
        )
        + shard.fpi_form_observations,
        form_history_proofs=tuple(
            row for row in source.merge.form_history_proofs if row.cik not in targets
        )
        + shard.form_history_proofs,
        acquisition_gaps=tuple(
            row for row in source.merge.acquisition_gaps if row.cik not in targets
        )
        + shard.acquisition_gaps,
        bootstrap_gaps=tuple(row for row in source.merge.bootstrap_gaps if row.cik not in targets)
        + shard.bootstrap_gaps,
        master=master,
    )
    return reconcile_cover_evidence_merge(patched).merge


def _dimension_equity_class(identity_anchor: str) -> str | None:
    prefix = "sec-cover-class:"
    if not identity_anchor.startswith(prefix):
        return None
    try:
        dimensions = json.loads(identity_anchor.removeprefix(prefix))
    except json.JSONDecodeError:
        return None
    if not isinstance(dimensions, list):
        return None
    classes: set[str] = set()
    for dimension in dimensions:
        if not isinstance(dimension, list) or len(dimension) != 2:
            continue
        member = re.sub(
            r"Member$",
            "",
            str(dimension[1]).rsplit(":", 1)[-1],
            flags=re.IGNORECASE,
        )
        for pattern in _DIMENSION_CLASS_PATTERNS:
            match = pattern.search(member)
            if match and match.group(1).casefold() not in {"common", "of", "ordinary", "stock"}:
                classes.add(match.group(1).casefold())
                break
    return next(iter(classes)) if len(classes) == 1 else None


def _semantic_equity_key(security: Security) -> str | None:
    title = " ".join(security.class_title.casefold().split())
    if is_explicit_non_common_security_title(title):
        return None
    if _COMMON_EQUITY_PATTERN.search(title):
        kind = "common-stock"
    elif _ORDINARY_EQUITY_PATTERN.search(title):
        kind = "ordinary-share"
    else:
        return None
    match = _EQUITY_CLASS_PATTERN.search(title)
    equity_class = (
        match.group(1).casefold() if match else _dimension_equity_class(security.identity_anchor)
    )
    qualifier: str | None = None
    if re.search(r"\bnon[- ]?voting\b", title):
        qualifier = "nonvoting"
    elif re.search(r"\bvariable\s+voting\b", title):
        qualifier = "variable-voting"
    elif re.search(r"\bsubordinate\s+voting\b", title):
        qualifier = "subordinate-voting"
    elif re.search(r"\bvoting\b", title):
        qualifier = "voting"
    return ":".join(
        (
            kind,
            *((f"class-{equity_class}",) if equity_class else ()),
            *((qualifier,) if qualifier else ()),
        )
    )


def reconcile_cover_evidence_merge(merged: CoverEvidenceMerge) -> CoverIdentityReconciliation:
    """Collapse filing wording drift without using a ticker as permanent identity."""
    symbols_by_security: dict[str, list[SymbolInterval]] = defaultdict(list)
    for symbol in merged.master.symbols:
        symbols_by_security[symbol.security_id].append(symbol)
    parent = {security.security_id: security.security_id for security in merged.master.securities}

    def find(security_id: str) -> str:
        while parent[security_id] != security_id:
            parent[security_id] = parent[parent[security_id]]
            security_id = parent[security_id]
        return security_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        parent[max(left_root, right_root)] = min(left_root, right_root)

    securities_by_id = {security.security_id: security for security in merged.master.securities}
    intervals_by_pair: dict[tuple[int, str, str], list[SymbolInterval]] = defaultdict(list)
    for symbol in merged.master.symbols:
        security = securities_by_id[symbol.security_id]
        if security.security_type == "common_stock":
            intervals_by_pair[(security.cik, symbol.ticker, symbol.exchange)].append(symbol)
    for intervals in intervals_by_pair.values():
        for index, left in enumerate(intervals):
            for right in intervals[index + 1 :]:
                left_end = left.valid_to or date.max
                right_end = right.valid_to or date.max
                if left.valid_from <= right_end and right.valid_from <= left_end:
                    union(left.security_id, right.security_id)
    component_sizes = Counter(find(security_id) for security_id in parent)

    equity_groups: dict[tuple[int, str], list[Security]] = defaultdict(list)
    untouched: list[Security] = []
    for security in merged.master.securities:
        component = find(security.security_id)
        key = _semantic_equity_key(security)
        if component_sizes[component] > 1 and security.security_type == "common_stock":
            key = f"common-stock:exact-symbol-overlap:{component}"
        elif key is None:
            untouched.append(security)
            continue
        equity_groups[(security.cik, key)].append(security)
    if any(not symbols_by_security[security.security_id] for security in merged.master.securities):
        raise EdgarPayloadError("cover identity reconciliation requires symbol evidence")

    remapped_ids: dict[str, str] = {}
    reconciled_securities = untouched
    collapsed_groups = 0
    ambiguous_groups = 0
    for (cik, key), securities in sorted(equity_groups.items()):
        symbols = [
            symbol
            for security in securities
            for symbol in symbols_by_security[security.security_id]
        ]
        pairs_by_start: dict[date, set[tuple[str, str]]] = defaultdict(set)
        for symbol in symbols:
            pairs_by_start[symbol.valid_from].add((symbol.ticker, symbol.exchange))
        distinct_pairs = {(symbol.ticker, symbol.exchange) for symbol in symbols}
        has_explicit_discriminator = ":" in key
        ambiguous = (
            len({security.domestic_flag for security in securities}) != 1
            or any(len(pairs) > 1 for pairs in pairs_by_start.values())
            or (len(distinct_pairs) > 1 and not has_explicit_discriminator)
        )
        if ambiguous:
            reconciled_securities.extend(securities)
            ambiguous_groups += 1
            continue
        identity_anchor = f"{COVER_IDENTITY_RECONCILIATION_VERSION}:{key}"
        security_id = mint_security_id(cik, identity_anchor)
        latest = max(
            securities,
            key=lambda security: (
                max(symbol.known_at for symbol in symbols_by_security[security.security_id]),
                security.evidence_pointer,
                security.security_id,
            ),
        )
        reconciled_securities.append(
            replace(
                latest,
                security_id=security_id,
                security_type=(
                    "common_stock"
                    if key.startswith(("common-stock", "ordinary-share"))
                    else "other"
                ),
                identity_anchor=identity_anchor,
            )
        )
        if len(securities) > 1:
            collapsed_groups += 1
        for security in securities:
            if security.security_id != security_id:
                remapped_ids[security.security_id] = security_id

    reconciled_symbols = _collapse_symbol_observations(
        replace(symbol, security_id=remapped_ids.get(symbol.security_id, symbol.security_id))
        for symbol in merged.master.symbols
    )
    master = build_security_master(reconciled_securities, reconciled_symbols)
    shares = tuple(
        replace(
            observation,
            security_id=remapped_ids.get(observation.security_id, observation.security_id),
        )
        for observation in merged.share_observations
    )
    reconciled = replace(merged, share_observations=shares, master=master)
    return CoverIdentityReconciliation(
        reconciled,
        collapsed_groups,
        len(remapped_ids),
        ambiguous_groups,
    )


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _terminal_form_payload(
    observations: tuple[CoverTerminalFormObservation, ...],
) -> list[dict[str, object]]:
    return [
        {
            "cik": row.cik,
            "accession": row.accession,
            "form": row.form,
            "accepted": row.accepted.astimezone(UTC).isoformat(),
            "evidence_pointer": row.evidence_pointer,
        }
        for row in observations
    ]


def _read_terminal_forms(
    payload: dict[str, object],
) -> tuple[CoverTerminalFormObservation, ...]:
    return tuple(
        CoverTerminalFormObservation(
            row["cik"],
            row["accession"],
            row["form"],
            datetime.fromisoformat(row["accepted"]),
            row["evidence_pointer"],
        )
        for row in payload.get("terminal_form_observations", [])
    )


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
        "fpi_form_observations": [
            {
                "cik": row.cik,
                "accession": row.accession,
                "form": row.form,
                "accepted": row.accepted.astimezone(UTC).isoformat(),
                "evidence_pointer": row.evidence_pointer,
            }
            for row in acquisition.fpi_form_observations
        ],
        "form_history_proofs": [
            {
                "cik": row.cik,
                "as_of": row.as_of.astimezone(UTC).isoformat(),
                "source_documents": list(row.source_documents),
                "evidence_pointer": row.evidence_pointer,
                "terminal_form_observations": _terminal_form_payload(
                    row.terminal_form_observations
                ),
            }
            for row in acquisition.form_history_proofs
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
        if not isinstance(master_snapshot_id, str) or not _SHA256_PATTERN.fullmatch(
            master_snapshot_id
        ):
            raise EdgarPayloadError("cover evidence master identity is invalid")
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
        fpi_form_observations = tuple(
            CoverFpiFormObservation(
                row["cik"],
                row["accession"],
                row["form"],
                datetime.fromisoformat(row["accepted"]),
                row["evidence_pointer"],
            )
            for row in payload["fpi_form_observations"]
        )
        form_history_proofs = tuple(
            CoverFormHistoryProof(
                row["cik"],
                datetime.fromisoformat(row["as_of"]),
                tuple(row["source_documents"]),
                row["evidence_pointer"],
                _read_terminal_forms(row),
            )
            for row in payload["form_history_proofs"]
        )
        acquisition_gaps = tuple(CoverAcquisitionGap(**row) for row in payload["acquisition_gaps"])
        bootstrap_gaps = tuple(CoverBootstrapGap(**row) for row in payload["bootstrap_gaps"])
        as_of = datetime.fromisoformat(payload["as_of"])
        requested_ciks = tuple(payload["requested_ciks"])
        plan_snapshot_id = payload["plan_snapshot_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EdgarPayloadError("cover evidence shard metadata is invalid") from exc
    if (
        not isinstance(plan_snapshot_id, str)
        or not _SHA256_PATTERN.fullmatch(plan_snapshot_id)
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
        or any(
            row.cik not in requested_ciks or row.accepted.astimezone(UTC) > as_of.astimezone(UTC)
            for row in fpi_form_observations
        )
        or len({row.cik for row in form_history_proofs}) != len(form_history_proofs)
        or any(
            row.cik not in requested_ciks or row.as_of.astimezone(UTC) != as_of.astimezone(UTC)
            for row in form_history_proofs
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
        fpi_form_observations,
        form_history_proofs,
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
        or any(row.cik not in requested for row in acquisition.fpi_form_observations)
        or any(row.cik not in requested for row in acquisition.form_history_proofs)
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
        _publish_directory(temporary, target)
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
    merged = CoverEvidenceMerge(
        rows[0].plan_snapshot_id,
        rows[0].as_of,
        expected,
        tuple(row.snapshot_id for row in rows),
        tuple(archive for row in rows for archive in row.archives),
        tuple(observation for row in rows for observation in row.share_observations),
        tuple(observation for row in rows for observation in row.fpi_form_observations),
        tuple(proof for row in rows for proof in row.form_history_proofs),
        tuple(gap for row in rows for gap in row.acquisition_gaps),
        tuple(gap for row in rows for gap in row.bootstrap_gaps),
        master,
    )
    return reconcile_cover_evidence_merge(merged).merge


def _merge_payload(
    merged: CoverEvidenceMerge,
    master_snapshot_id: str,
) -> dict[str, object]:
    return {
        "version": COVER_MERGE_VERSION,
        "plan_snapshot_id": merged.plan_snapshot_id,
        "as_of": merged.as_of.astimezone(UTC).isoformat(),
        "requested_ciks": list(merged.requested_ciks),
        "shard_snapshot_ids": list(merged.shard_snapshot_ids),
        "archives": [asdict(row) for row in merged.archives],
        "share_observations": [
            {
                "security_id": row.security_id,
                "cik": row.cik,
                "accepted": row.accepted.astimezone(UTC).isoformat(),
                "shares_outstanding": str(row.shares_outstanding),
                "evidence_pointer": row.evidence_pointer,
            }
            for row in merged.share_observations
        ],
        "fpi_form_observations": [
            {
                "cik": row.cik,
                "accession": row.accession,
                "form": row.form,
                "accepted": row.accepted.astimezone(UTC).isoformat(),
                "evidence_pointer": row.evidence_pointer,
            }
            for row in merged.fpi_form_observations
        ],
        "form_history_proofs": [
            {
                "cik": row.cik,
                "as_of": row.as_of.astimezone(UTC).isoformat(),
                "source_documents": list(row.source_documents),
                "evidence_pointer": row.evidence_pointer,
                "terminal_form_observations": _terminal_form_payload(
                    row.terminal_form_observations
                ),
            }
            for row in merged.form_history_proofs
        ],
        "acquisition_gaps": [asdict(row) for row in merged.acquisition_gaps],
        "bootstrap_gaps": [asdict(row) for row in merged.bootstrap_gaps],
        "master_snapshot_id": master_snapshot_id,
    }


def _read_merge_payload(path: Path) -> tuple[dict[str, object], str]:
    try:
        payload = json.loads((path / "complete-evidence.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("complete cover evidence metadata is unreadable") from exc
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    if path.name != snapshot_id or payload.get("version") not in _SUPPORTED_COVER_MERGE_VERSIONS:
        raise EdgarPayloadError("complete cover evidence identity is invalid")
    return payload, snapshot_id


def read_cover_evidence_snapshot(path: str | Path) -> CoverEvidenceSnapshot:
    """Verify and reopen one complete cover-evidence package."""
    root = Path(path)
    payload, snapshot_id = _read_merge_payload(root)
    try:
        master_snapshot_id = payload["master_snapshot_id"]
        if not isinstance(master_snapshot_id, str) or not _SHA256_PATTERN.fullmatch(
            master_snapshot_id
        ):
            raise EdgarPayloadError("complete cover evidence master identity is invalid")
        master = read_security_master_snapshot(root / "master" / "snapshots" / master_snapshot_id)
        as_of = datetime.fromisoformat(payload["as_of"])
        requested_ciks = tuple(payload["requested_ciks"])
        shard_snapshot_ids = tuple(payload["shard_snapshot_ids"])
        archives = tuple(CoverArchiveRecord(**row) for row in payload["archives"])
        shares = tuple(
            CoverShareObservation(
                row["security_id"],
                row["cik"],
                datetime.fromisoformat(row["accepted"]),
                Decimal(row["shares_outstanding"]),
                row["evidence_pointer"],
            )
            for row in payload["share_observations"]
        )
        fpi_forms = tuple(
            CoverFpiFormObservation(
                row["cik"],
                row["accession"],
                row["form"],
                datetime.fromisoformat(row["accepted"]),
                row["evidence_pointer"],
            )
            for row in payload["fpi_form_observations"]
        )
        form_history_proofs = tuple(
            CoverFormHistoryProof(
                row["cik"],
                datetime.fromisoformat(row["as_of"]),
                tuple(row["source_documents"]),
                row["evidence_pointer"],
                _read_terminal_forms(row),
            )
            for row in payload["form_history_proofs"]
        )
        acquisition_gaps = tuple(CoverAcquisitionGap(**row) for row in payload["acquisition_gaps"])
        bootstrap_gaps = tuple(CoverBootstrapGap(**row) for row in payload["bootstrap_gaps"])
        plan_snapshot_id = payload["plan_snapshot_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EdgarPayloadError("complete cover evidence metadata is invalid") from exc
    requested = frozenset(requested_ciks)
    security_ids = {row.security_id for row in master.securities}
    if (
        not isinstance(plan_snapshot_id, str)
        or not _SHA256_PATTERN.fullmatch(plan_snapshot_id)
        or as_of.tzinfo is None
        or tuple(sorted(requested)) != requested_ciks
        or any(not isinstance(cik, int) or cik <= 0 for cik in requested_ciks)
        or not shard_snapshot_ids
        or tuple(sorted(set(shard_snapshot_ids))) != shard_snapshot_ids
        or any(
            not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value)
            for value in shard_snapshot_ids
        )
        or any(row.cik not in requested for row in master.securities)
        or any(
            row.cik not in requested
            or row.security_id not in security_ids
            or row.accepted.astimezone(UTC) > as_of.astimezone(UTC)
            for row in shares
        )
        or any(
            row.cik not in requested or row.accepted.astimezone(UTC) > as_of.astimezone(UTC)
            for row in fpi_forms
        )
        or len({row.cik for row in form_history_proofs}) != len(form_history_proofs)
        or any(
            row.cik not in requested or row.as_of.astimezone(UTC) != as_of.astimezone(UTC)
            for row in form_history_proofs
        )
        or any(
            row.cik not in requested
            or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", row.accession)
            or len(row.archive_snapshot_id) != 64
            or len(row.primary_sha256) != 64
            for row in archives
        )
        or any(row.cik not in requested for row in acquisition_gaps)
        or any(row.cik not in requested for row in bootstrap_gaps)
    ):
        raise EdgarPayloadError("complete cover evidence provenance is invalid")
    merged = CoverEvidenceMerge(
        plan_snapshot_id,
        as_of.astimezone(UTC),
        requested_ciks,
        shard_snapshot_ids,
        archives,
        shares,
        fpi_forms,
        form_history_proofs,
        acquisition_gaps,
        bootstrap_gaps,
        master,
    )
    return CoverEvidenceSnapshot(snapshot_id, root, merged, master_snapshot_id, True)


def materialize_cover_evidence_merge(
    merged: CoverEvidenceMerge,
    output_root: str | Path,
) -> CoverEvidenceSnapshot:
    """Persist the exact merged master, shares and gaps as one immutable package."""
    if (
        not _SHA256_PATTERN.fullmatch(merged.plan_snapshot_id)
        or merged.as_of.tzinfo is None
        or tuple(sorted(set(merged.requested_ciks))) != merged.requested_ciks
        or not merged.requested_ciks
        or tuple(sorted(set(merged.shard_snapshot_ids))) != merged.shard_snapshot_ids
        or not merged.shard_snapshot_ids
        or any(not _SHA256_PATTERN.fullmatch(value) for value in merged.shard_snapshot_ids)
    ):
        raise EdgarPayloadError("complete cover evidence requires exact merged inputs")
    root = (
        Path(output_root)
        / "security-bootstrap"
        / "complete-evidence"
        / merged.plan_snapshot_id
        / merged.as_of.date().isoformat()
        / "snapshots"
    )
    temporary = root / f".pending.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        master_snapshot = materialize_security_master(merged.master, temporary / "master")
        payload = _merge_payload(merged, master_snapshot.snapshot_id)
        snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
        target = root / snapshot_id
        if target.exists():
            shutil.rmtree(temporary, ignore_errors=True)
            existing = read_cover_evidence_snapshot(target)
            if _read_merge_payload(existing.output_dir)[0] != payload:
                raise EdgarPayloadError("complete cover evidence cache conflicts with the input")
            return existing
        (temporary / "complete-evidence.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        _publish_directory(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    created = read_cover_evidence_snapshot(target)
    return CoverEvidenceSnapshot(
        created.snapshot_id,
        created.output_dir,
        created.merge,
        created.master_snapshot_id,
        False,
    )
