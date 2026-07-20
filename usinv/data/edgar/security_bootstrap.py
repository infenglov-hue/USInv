"""Discovery-only SEC ticker joins and filing-backed security-master bootstrap."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal

from usinv.data.edgar.client import EdgarDocument, EdgarPayloadError
from usinv.data.edgar.filing_xbrl import (
    FilingParseResult,
    extract_cover_security_classes,
    security_evidence_from_cover,
)
from usinv.data.edgar.securities import (
    Security,
    SecurityMaster,
    SecurityMasterError,
    SymbolInterval,
    build_security_master,
    normalize_exchange,
    normalize_ticker,
)
from usinv.data.edgar.submissions import SubmissionFeed, SubmissionFiling
from usinv.data.listings import AlphaListingSnapshot

SEC_TICKER_FIELDS: Final = ("cik", "name", "ticker", "exchange")
DISCOVERY_VERSION: Final = "usinv-sec-filing-discovery-v2"
COVER_FORMS: Final = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "8-K",
        "8-K/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "S-1",
        "S-1/A",
        "F-1",
        "F-1/A",
    }
)
DiscoveryStatus = Literal[
    "discovered",
    "unmapped",
    "ambiguous",
    "unsupported_exchange",
    "unsupported_asset_type",
]
_DISCOVERY_STATUSES: Final = frozenset(
    {"discovered", "unmapped", "ambiguous", "unsupported_exchange", "unsupported_asset_type"}
)


@dataclass(frozen=True, slots=True)
class SecTickerAssociation:
    cik: int
    name: str
    ticker: str
    exchange: str
    row_number: int


@dataclass(frozen=True, slots=True)
class SecTickerAssociationSnapshot:
    observed_at: datetime
    source_url: str
    source_sha256: str
    rows: tuple[SecTickerAssociation, ...]
    unusable_rows: int

    def __post_init__(self) -> None:
        if (
            self.observed_at.tzinfo is None
            or len(self.source_sha256) != 64
            or not self.rows
            or self.unusable_rows < 0
        ):
            raise EdgarPayloadError("SEC ticker association snapshot provenance is incomplete")


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EdgarPayloadError(f"SEC ticker association {field} must be an array")
    return value


def parse_sec_ticker_associations(document: EdgarDocument) -> SecTickerAssociationSnapshot:
    """Parse the current SEC discovery file without granting identity confidence."""
    fields = tuple(_sequence(document.payload.get("fields"), "fields"))
    if fields != SEC_TICKER_FIELDS:
        raise EdgarPayloadError("SEC ticker association fields drifted")
    raw_rows = _sequence(document.payload.get("data"), "data")
    rows: list[SecTickerAssociation] = []
    seen: set[tuple[int, str, str]] = set()
    unusable_rows = 0
    for row_number, raw in enumerate(raw_rows, start=1):
        values = _sequence(raw, f"data[{row_number}]")
        if len(values) != len(SEC_TICKER_FIELDS):
            raise EdgarPayloadError("SEC ticker association row width drifted")
        cik_raw, name_raw, ticker_raw, exchange_raw = values
        try:
            cik = int(cik_raw)
        except (TypeError, ValueError) as exc:
            raise EdgarPayloadError("SEC ticker association CIK is invalid") from exc
        if cik <= 0 or not isinstance(name_raw, str) or not name_raw.strip():
            raise EdgarPayloadError("SEC ticker association identity is incomplete")
        if (
            ticker_raw is None
            or exchange_raw is None
            or (
                isinstance(ticker_raw, str)
                and isinstance(exchange_raw, str)
                and (not ticker_raw.strip() or not exchange_raw.strip())
            )
        ):
            unusable_rows += 1
            continue
        if not isinstance(ticker_raw, str) or not isinstance(exchange_raw, str):
            raise EdgarPayloadError("SEC ticker association symbol fields must be text")
        try:
            ticker = normalize_ticker(ticker_raw)
        except SecurityMasterError as exc:
            raise EdgarPayloadError("SEC ticker association ticker is invalid") from exc
        exchange = " ".join(exchange_raw.strip().split())
        if not exchange:
            raise EdgarPayloadError("SEC ticker association exchange is empty")
        identity = (cik, ticker, exchange.casefold())
        if identity in seen:
            raise EdgarPayloadError("SEC ticker association contains an exact duplicate")
        seen.add(identity)
        rows.append(SecTickerAssociation(cik, name_raw.strip(), ticker, exchange, row_number))
    if not rows:
        raise EdgarPayloadError("SEC ticker association data is empty")
    return SecTickerAssociationSnapshot(
        document.validated_at.astimezone(UTC),
        document.url,
        document.content_sha256,
        tuple(rows),
        unusable_rows,
    )


@dataclass(frozen=True, slots=True)
class FilingDiscoveryRow:
    ticker: str
    raw_exchange: str
    normalized_exchange: str | None
    asset_type: str
    listing_evidence_pointer: str
    status: DiscoveryStatus
    candidate_ciks: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FilingDiscoveryPlan:
    listing_as_of: date
    listing_snapshot_id: str
    association_source_sha256: str
    association_observed_at: datetime
    rows: tuple[FilingDiscoveryRow, ...]
    version: str = DISCOVERY_VERSION
    association_unusable_rows: int = 0

    def __post_init__(self) -> None:
        if (
            self.association_observed_at.tzinfo is None
            or len(self.listing_snapshot_id) != 64
            or len(self.association_source_sha256) != 64
            or not self.rows
            or self.association_unusable_rows < 0
        ):
            raise EdgarPayloadError("filing discovery plan provenance is incomplete")
        for row in self.rows:
            if row.status not in _DISCOVERY_STATUSES:
                raise EdgarPayloadError("filing discovery row status is invalid")
            if tuple(sorted(set(row.candidate_ciks))) != row.candidate_ciks or any(
                cik <= 0 for cik in row.candidate_ciks
            ):
                raise EdgarPayloadError("filing discovery CIK candidates are invalid")
            expected_candidates = {
                "discovered": 1,
                "unmapped": 0,
                "unsupported_exchange": 0,
                "unsupported_asset_type": 0,
            }.get(row.status)
            if expected_candidates is not None and len(row.candidate_ciks) != expected_candidates:
                raise EdgarPayloadError("filing discovery status contradicts its CIK candidates")
            if row.status == "ambiguous" and len(row.candidate_ciks) < 2:
                raise EdgarPayloadError("ambiguous filing discovery needs multiple CIK candidates")
            if row.ticker != normalize_ticker(row.ticker):
                raise EdgarPayloadError("filing discovery ticker is not normalized")
            if (
                row.normalized_exchange is not None
                and row.normalized_exchange != normalize_exchange(row.normalized_exchange)
            ):
                raise EdgarPayloadError("filing discovery exchange is not normalized")

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(
            json.dumps(_plan_payload(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def ciks(self) -> tuple[int, ...]:
        return tuple(
            sorted(
                {
                    row.candidate_ciks[0]
                    for row in self.rows
                    if row.status == "discovered" and len(row.candidate_ciks) == 1
                }
            )
        )

    @property
    def identity_gaps(self) -> tuple[FilingDiscoveryRow, ...]:
        return tuple(row for row in self.rows if row.status in {"unmapped", "ambiguous"})


def build_filing_discovery_plan(
    listings: AlphaListingSnapshot,
    associations: SecTickerAssociationSnapshot,
) -> FilingDiscoveryPlan:
    """Locate CIKs to inspect; results are deliberately ineligible as identity mappings."""
    index: dict[tuple[str, str], set[int]] = defaultdict(set)
    for item in associations.rows:
        try:
            exchange = normalize_exchange(item.exchange)
        except SecurityMasterError:
            continue
        index[(item.ticker, exchange)].add(item.cik)

    rows: list[FilingDiscoveryRow] = []
    for listing in sorted(
        (row for row in listings.rows if row.state == "active"),
        key=lambda row: (row.exchange, row.symbol, row.row_number),
    ):
        pointer = f"alpha-vantage://{listing.source_sha256}/{listing.row_number}"
        try:
            exchange = normalize_exchange(listing.exchange)
        except SecurityMasterError:
            rows.append(
                FilingDiscoveryRow(
                    listing.symbol,
                    listing.exchange,
                    None,
                    listing.asset_type,
                    pointer,
                    "unsupported_exchange",
                    (),
                )
            )
            continue
        if listing.asset_type.casefold() != "stock":
            rows.append(
                FilingDiscoveryRow(
                    listing.symbol,
                    listing.exchange,
                    exchange,
                    listing.asset_type,
                    pointer,
                    "unsupported_asset_type",
                    (),
                )
            )
            continue
        candidates = tuple(sorted(index.get((listing.symbol, exchange), ())))
        status: DiscoveryStatus
        if len(candidates) == 1:
            status = "discovered"
        elif candidates:
            status = "ambiguous"
        else:
            status = "unmapped"
        rows.append(
            FilingDiscoveryRow(
                listing.symbol,
                listing.exchange,
                exchange,
                listing.asset_type,
                pointer,
                status,
                candidates,
            )
        )
    return FilingDiscoveryPlan(
        listings.as_of,
        listings.snapshot_id,
        associations.source_sha256,
        associations.observed_at,
        tuple(rows),
        association_unusable_rows=associations.unusable_rows,
    )


def _discovery_row_payload(row: FilingDiscoveryRow) -> dict[str, object]:
    return {
        "ticker": row.ticker,
        "raw_exchange": row.raw_exchange,
        "normalized_exchange": row.normalized_exchange,
        "asset_type": row.asset_type,
        "listing_evidence_pointer": row.listing_evidence_pointer,
        "status": row.status,
        "candidate_ciks": list(row.candidate_ciks),
    }


def _plan_payload(plan: FilingDiscoveryPlan) -> dict[str, object]:
    return {
        "version": plan.version,
        "listing_as_of": plan.listing_as_of.isoformat(),
        "listing_snapshot_id": plan.listing_snapshot_id,
        "association_source_sha256": plan.association_source_sha256,
        "association_observed_at": plan.association_observed_at.astimezone(UTC).isoformat(),
        "association_unusable_rows": plan.association_unusable_rows,
        "identity_policy": "discovery_only_never_mapping_evidence",
        "rows": [_discovery_row_payload(row) for row in plan.rows],
    }


@dataclass(frozen=True, slots=True)
class FilingDiscoveryArtifact:
    snapshot_id: str
    output_dir: Path
    rows: int
    discovered_ciks: int
    identity_gaps: int
    association_unusable_rows: int
    from_cache: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_discovery_artifact(path: Path, plan: FilingDiscoveryPlan) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        plan_path = path / "discovery.json"
        plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("filing discovery artifact is unreadable") from exc
    if (
        manifest.get("snapshot_id") != plan.snapshot_id
        or not plan_path.is_file()
        or _sha256(plan_path) != manifest.get("artifact", {}).get("sha256")
        or plan_payload != _plan_payload(plan)
    ):
        raise EdgarPayloadError("filing discovery artifact failed verification")


def materialize_filing_discovery_plan(
    plan: FilingDiscoveryPlan,
    output_root: str | Path,
) -> FilingDiscoveryArtifact:
    """Persist a content-addressed discovery-only plan without provider credentials."""
    root = Path(output_root) / "security-bootstrap" / "discovery" / plan.listing_as_of.isoformat()
    target = root / plan.snapshot_id
    if target.exists():
        _verify_discovery_artifact(target, plan)
        return FilingDiscoveryArtifact(
            plan.snapshot_id,
            target,
            len(plan.rows),
            len(plan.ciks),
            len(plan.identity_gaps),
            plan.association_unusable_rows,
            True,
        )
    temporary = root / f".{plan.snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        plan_path = temporary / "discovery.json"
        plan_path.write_text(
            json.dumps(_plan_payload(plan), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest = {
            "schema_version": 1,
            "snapshot_id": plan.snapshot_id,
            "counts": {
                "rows": len(plan.rows),
                "discovered_ciks": len(plan.ciks),
                "identity_gaps": len(plan.identity_gaps),
                "association_unusable_rows": plan.association_unusable_rows,
            },
            "artifact": {"path": "discovery.json", "sha256": _sha256(plan_path)},
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _verify_discovery_artifact(target, plan)
    return FilingDiscoveryArtifact(
        plan.snapshot_id,
        target,
        len(plan.rows),
        len(plan.ciks),
        len(plan.identity_gaps),
        plan.association_unusable_rows,
        False,
    )


def read_filing_discovery_plan(path: str | Path) -> FilingDiscoveryPlan:
    """Open and verify one immutable filing-discovery plan."""
    root = Path(path)
    try:
        payload = json.loads((root / "discovery.json").read_text(encoding="utf-8"))
        if payload["version"] != DISCOVERY_VERSION:
            raise ValueError("version")
        rows = tuple(
            FilingDiscoveryRow(
                ticker=row["ticker"],
                raw_exchange=row["raw_exchange"],
                normalized_exchange=row["normalized_exchange"],
                asset_type=row["asset_type"],
                listing_evidence_pointer=row["listing_evidence_pointer"],
                status=row["status"],
                candidate_ciks=tuple(row["candidate_ciks"]),
            )
            for row in payload["rows"]
        )
        plan = FilingDiscoveryPlan(
            date.fromisoformat(payload["listing_as_of"]),
            payload["listing_snapshot_id"],
            payload["association_source_sha256"],
            datetime.fromisoformat(payload["association_observed_at"]),
            rows,
            association_unusable_rows=payload["association_unusable_rows"],
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("filing discovery artifact is invalid") from exc
    if root.name != plan.snapshot_id:
        raise EdgarPayloadError("filing discovery identity does not match its directory")
    _verify_discovery_artifact(root, plan)
    return plan


def select_cover_filings(
    feed: SubmissionFeed,
    *,
    as_of: datetime,
    maximum: int = 4,
) -> tuple[SubmissionFiling, ...]:
    """Select recent filing candidates whose accepted instant is inside the PIT boundary."""
    if as_of.tzinfo is None or maximum <= 0:
        raise EdgarPayloadError("cover filing selection boundary is invalid")
    cutoff = as_of.astimezone(UTC)
    candidates = [
        filing
        for filing in feed.filings
        if filing.accepted <= cutoff and filing.form.upper() in COVER_FORMS
    ]
    ordered = sorted(candidates, key=lambda row: (row.accepted, row.accession), reverse=True)
    event_reports = [row for row in ordered if row.form.upper().removesuffix("/A") == "8-K"]
    structural = [row for row in ordered if row.form.upper().removesuffix("/A") != "8-K"]
    if maximum >= 2 and event_reports and structural:
        selected = structural[: maximum - 1] + event_reports[:1]
        return tuple(sorted(selected, key=lambda row: (row.accepted, row.accession), reverse=True))
    return tuple(ordered[:maximum])


@dataclass(frozen=True, slots=True)
class CoverFilingEvidence:
    filing: SubmissionFiling
    parsed: FilingParseResult
    domestic_flag: bool
    allowed_pairs: frozenset[tuple[str, str]] | None = None


@dataclass(frozen=True, slots=True)
class CoverBootstrapGap:
    cik: int
    accession: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class CoverSecurityBootstrap:
    master: SecurityMaster
    filings: int
    cover_classes: int
    gaps: tuple[CoverBootstrapGap, ...]


def _collapse_symbol_observations(
    observations: Iterable[SymbolInterval],
) -> tuple[SymbolInterval, ...]:
    by_security: dict[str, list[SymbolInterval]] = defaultdict(list)
    for item in observations:
        by_security[item.security_id].append(item)
    output: list[SymbolInterval] = []
    for security_id in sorted(by_security):
        rows = sorted(
            by_security[security_id],
            key=lambda row: (row.valid_from, row.known_at, row.exchange, row.ticker),
        )
        active: SymbolInterval | None = None
        for item in rows:
            if active is None:
                active = item
                continue
            if (active.ticker, active.exchange) == (item.ticker, item.exchange):
                continue
            if item.valid_from <= active.valid_from:
                output.extend((active, item))
                active = None
                continue
            output.append(replace(active, valid_to=item.valid_from))
            active = item
        if active is not None:
            output.append(active)
    return tuple(output)


def build_cover_security_master(
    evidence: Iterable[CoverFilingEvidence],
    *,
    as_of: datetime,
) -> CoverSecurityBootstrap:
    """Build high-confidence forward intervals exclusively from filing cover facts."""
    if as_of.tzinfo is None:
        raise EdgarPayloadError("cover security bootstrap cutoff must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    securities: dict[str, Security] = {}
    symbols: list[SymbolInterval] = []
    gaps: list[CoverBootstrapGap] = []
    filings = 0
    cover_count = 0
    for item in sorted(evidence, key=lambda row: (row.filing.accepted, row.filing.accession)):
        filings += 1
        if item.filing.accepted > cutoff:
            raise EdgarPayloadError("future filing reached the cover security bootstrap")
        covers = extract_cover_security_classes(item.parsed)
        if item.allowed_pairs is not None:
            admitted = []
            for cover in covers:
                try:
                    pair = (normalize_ticker(cover.ticker), normalize_exchange(cover.exchange))
                except SecurityMasterError:
                    continue
                if pair in item.allowed_pairs:
                    admitted.append(cover)
            covers = tuple(admitted)
        if not covers:
            gaps.append(
                CoverBootstrapGap(
                    item.filing.cik,
                    item.filing.accession,
                    (
                        "cover_not_in_discovery_plan"
                        if item.allowed_pairs is not None
                        else "missing_cover_class"
                    ),
                    (
                        "filing cover classes do not match a discovered listing pair"
                        if item.allowed_pairs is not None
                        else "filing has no unique ticker/exchange/class cover tuple"
                    ),
                )
            )
            continue
        for cover in covers:
            try:
                security, symbol = security_evidence_from_cover(
                    item.filing,
                    cover,
                    domestic_flag=item.domestic_flag,
                )
            except SecurityMasterError as exc:
                gaps.append(
                    CoverBootstrapGap(
                        item.filing.cik,
                        item.filing.accession,
                        "unsupported_cover_identity",
                        str(exc),
                    )
                )
                continue
            cover_count += 1
            prior = securities.get(security.security_id)
            if prior is not None and (
                prior.cik,
                prior.class_title,
                prior.security_type,
                prior.domestic_flag,
                prior.identity_anchor,
            ) != (
                security.cik,
                security.class_title,
                security.security_type,
                security.domestic_flag,
                security.identity_anchor,
            ):
                raise EdgarPayloadError("filing cover evidence conflicts for one security ID")
            securities.setdefault(security.security_id, security)
            symbols.append(symbol)
    master = build_security_master(securities.values(), _collapse_symbol_observations(symbols))
    return CoverSecurityBootstrap(master, filings, cover_count, tuple(gaps))
