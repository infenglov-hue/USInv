"""Event-sourced security identity and date-valid ticker mapping."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.data.edgar.client import EdgarError

SECURITY_MASTER_VERSION: Final = "usinv-security-master-v1"
Confidence = Literal["low", "medium", "high"]
EvidenceScope = Literal["historical_interval", "live_edge_current", "recovery_only"]
MappingStatus = Literal["mapped", "unmapped", "quarantined"]
_CONFIDENCE_ORDER: Final = {"low": 0, "medium": 1, "high": 2}
_NON_COMMON_SECURITY_TITLE_PATTERN: Final = re.compile(
    r"\b(?:warrants?|depositary shares?|preferred(?:\s+\w+){0,3}\s+"
    r"(?:stock|shares?|securities|units?|lp)|notes?|bonds?)\b",
    re.IGNORECASE,
)
_NON_COMMON_SECURITY_RIGHT_PATTERN: Final = re.compile(
    r"^(?:series\s+\w+\s+)?(?:subscription\s+)?rights?\b|\bright to purchase\b",
    re.IGNORECASE,
)
_SECURITY_UNIT_PATTERN: Final = re.compile(r"\bunits?\b", re.IGNORECASE)
_COMMON_UNIT_PATTERN: Final = re.compile(r"\bcommon units?\b", re.IGNORECASE)
_EXCHANGE_ALIASES: Final = {
    "NASDAQ": "NASDAQ",
    "NASDAQ GLOBAL SELECT": "NASDAQ",
    "NASDAQ GLOBAL MARKET": "NASDAQ",
    "NASDAQ CAPITAL MARKET": "NASDAQ",
    "THE NASDAQ STOCK MARKET LLC": "NASDAQ",
    "NYSE": "NYSE",
    "NEW YORK STOCK EXCHANGE": "NYSE",
    "NYSE AMERICAN": "NYSEAMERICAN",
    "NYSE AMERICAN LLC": "NYSEAMERICAN",
    "NYSE MKT": "NYSEAMERICAN",
    "AMEX": "NYSEAMERICAN",
    "NYSEAMERICAN": "NYSEAMERICAN",
}


class SecurityMasterError(EdgarError):
    """Raised when identity evidence cannot form a safe security master."""


def normalize_ticker(value: str) -> str:
    """Normalize a vendor ticker without treating it as permanent identity."""
    ticker = value.strip().upper().replace(" ", "-")
    if not ticker or len(ticker) > 32 or any(character in ticker for character in "/\\"):
        raise SecurityMasterError(f"invalid ticker: {value!r}")
    return ticker


def normalize_exchange(value: str) -> str:
    """Normalize the three v1 US exchanges and reject unsupported venues."""
    exchange = " ".join(value.strip().upper().split())
    try:
        return _EXCHANGE_ALIASES[exchange]
    except KeyError as exc:
        raise SecurityMasterError(f"unsupported exchange: {value!r}") from exc


def is_explicit_non_common_security_title(title: str) -> bool:
    """Return true only when a security-class title explicitly names a non-common line."""
    normalized = " ".join(title.strip().split())
    if not normalized:
        return False
    if _NON_COMMON_SECURITY_TITLE_PATTERN.search(normalized):
        return True
    if _NON_COMMON_SECURITY_RIGHT_PATTERN.search(normalized):
        return True
    return bool(_SECURITY_UNIT_PATTERN.search(normalized)) and not (
        _COMMON_UNIT_PATTERN.search(normalized)
    )


def mint_security_id(cik: int, identity_anchor: str) -> str:
    """Mint a stable class identity from non-ticker authoritative evidence."""
    anchor = identity_anchor.strip()
    if cik <= 0 or not anchor:
        raise SecurityMasterError("security identity requires a positive CIK and anchor")
    lowered = anchor.casefold()
    if lowered.startswith(("ticker:", "symbol:")):
        raise SecurityMasterError("a ticker cannot be the immutable security identity anchor")
    digest = hashlib.sha256(f"{cik}\0{anchor}".encode()).hexdigest()[:24]
    return f"USINVSEC-{digest}"


@dataclass(frozen=True, slots=True)
class Security:
    """One immutable tradeable class linked to, but distinct from, a CIK."""

    security_id: str
    cik: int
    class_title: str
    security_type: str
    domestic_flag: bool
    identity_anchor: str
    source: str
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class SymbolInterval:
    """One half-open ticker/exchange validity interval with evidence."""

    security_id: str
    ticker: str
    exchange: str
    valid_from: date
    valid_to: date | None
    source: str
    confidence: Confidence
    evidence_pointer: str
    known_at: datetime
    scope: EvidenceScope

    def contains(self, session: date) -> bool:
        return self.valid_from <= session and (self.valid_to is None or session < self.valid_to)


def current_sec_symbol(
    *,
    security_id: str,
    ticker: str,
    exchange: str,
    observed_at: datetime,
    evidence_pointer: str,
) -> SymbolInterval:
    """Create live-edge SEC evidence that is forbidden from backfilling history."""
    if observed_at.tzinfo is None:
        raise SecurityMasterError("current SEC symbol observation must be timezone-aware")
    return SymbolInterval(
        security_id=security_id,
        ticker=normalize_ticker(ticker),
        exchange=normalize_exchange(exchange),
        valid_from=observed_at.astimezone(UTC).date(),
        valid_to=None,
        source="sec_submissions_current",
        confidence="medium",
        evidence_pointer=evidence_pointer,
        known_at=observed_at.astimezone(UTC),
        scope="live_edge_current",
    )


@dataclass(frozen=True, slots=True)
class MappingIssue:
    """A collision or overlap that must remain visible and quarantined."""

    issue_id: str
    kind: str
    ticker: str
    exchange: str
    valid_from: date
    valid_to: date | None
    security_ids: tuple[str, ...]
    evidence_pointers: tuple[str, ...]
    detail: str

    def contains(self, session: date) -> bool:
        return self.valid_from <= session and (self.valid_to is None or session < self.valid_to)


@dataclass(frozen=True, slots=True)
class MappingResult:
    status: MappingStatus
    ticker: str
    exchange: str
    session: date
    security_id: str | None
    candidate_security_ids: tuple[str, ...]
    evidence_pointers: tuple[str, ...]
    issue_ids: tuple[str, ...]


def _interval_end(value: date | None) -> date:
    return value or date.max


def _overlap(left: SymbolInterval, right: SymbolInterval) -> tuple[date, date | None] | None:
    start = max(left.valid_from, right.valid_from)
    end_value = min(_interval_end(left.valid_to), _interval_end(right.valid_to))
    if start >= end_value:
        return None
    return start, None if end_value == date.max else end_value


def _issue(
    kind: str,
    left: SymbolInterval,
    right: SymbolInterval,
    overlap: tuple[date, date | None],
    detail: str,
) -> MappingIssue:
    security_ids = tuple(sorted({left.security_id, right.security_id}))
    pointers = tuple(sorted({left.evidence_pointer, right.evidence_pointer}))
    payload = {
        "kind": kind,
        "ticker": left.ticker if left.ticker == right.ticker else "*",
        "exchange": left.exchange,
        "valid_from": overlap[0].isoformat(),
        "valid_to": overlap[1].isoformat() if overlap[1] else None,
        "security_ids": security_ids,
        "evidence_pointers": pointers,
    }
    issue_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return MappingIssue(
        issue_id=issue_id,
        kind=kind,
        ticker=payload["ticker"],
        exchange=left.exchange,
        valid_from=overlap[0],
        valid_to=overlap[1],
        security_ids=security_ids,
        evidence_pointers=pointers,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class SecurityMaster:
    """Validated immutable security and symbol evidence."""

    securities: tuple[Security, ...]
    symbols: tuple[SymbolInterval, ...]
    issues: tuple[MappingIssue, ...]
    version: str = SECURITY_MASTER_VERSION

    def resolve(
        self,
        ticker: str,
        exchange: str,
        session: date,
        *,
        minimum_confidence: Confidence = "high",
        required_security_type: str | None = None,
    ) -> MappingResult:
        normalized_ticker = normalize_ticker(ticker)
        normalized_exchange = normalize_exchange(exchange)
        threshold = _CONFIDENCE_ORDER[minimum_confidence]
        admitted_security_ids = {
            security.security_id
            for security in self.securities
            if required_security_type is None or security.security_type == required_security_type
        }
        evidence = tuple(
            interval
            for interval in self.symbols
            if interval.ticker == normalized_ticker
            and interval.exchange == normalized_exchange
            and interval.contains(session)
            and _CONFIDENCE_ORDER[interval.confidence] >= threshold
            and interval.scope != "recovery_only"
            and interval.security_id in admitted_security_ids
        )
        candidates = tuple(sorted({interval.security_id for interval in evidence}))
        active_issues = tuple(
            issue
            for issue in self.issues
            if issue.exchange == normalized_exchange
            and issue.contains(session)
            and (
                issue.ticker in {"*", normalized_ticker}
                or bool(set(issue.security_ids).intersection(candidates))
            )
            and (
                required_security_type is None
                or issue.kind != "ticker_collision"
                or len(set(issue.security_ids).intersection(candidates)) > 1
            )
        )
        pointers = tuple(sorted({item.evidence_pointer for item in evidence}))
        if len(candidates) == 1 and not active_issues:
            return MappingResult(
                "mapped",
                normalized_ticker,
                normalized_exchange,
                session,
                candidates[0],
                candidates,
                pointers,
                (),
            )
        return MappingResult(
            "quarantined" if candidates or active_issues else "unmapped",
            normalized_ticker,
            normalized_exchange,
            session,
            None,
            candidates,
            pointers,
            tuple(item.issue_id for item in active_issues),
        )


def build_security_master(
    securities: Iterable[Security],
    symbols: Iterable[SymbolInterval],
) -> SecurityMaster:
    """Validate identity evidence and materialize every collision as an issue."""
    security_rows = tuple(sorted(securities, key=lambda item: item.security_id))
    by_id: dict[str, Security] = {}
    for security in security_rows:
        if security.security_id in by_id and by_id[security.security_id] != security:
            raise SecurityMasterError(f"conflicting security identity: {security.security_id}")
        if security.cik <= 0 or not security.identity_anchor or not security.evidence_pointer:
            raise SecurityMasterError("security identity evidence is incomplete")
        if mint_security_id(security.cik, security.identity_anchor) != security.security_id:
            raise SecurityMasterError("security_id does not match its immutable identity anchor")
        by_id[security.security_id] = security
    security_rows = tuple(by_id[key] for key in sorted(by_id))

    symbol_rows: list[SymbolInterval] = []
    for interval in symbols:
        if interval.security_id not in by_id:
            raise SecurityMasterError(f"symbol references unknown security: {interval.security_id}")
        if interval.known_at.tzinfo is None:
            raise SecurityMasterError("symbol evidence known_at must be timezone-aware")
        if interval.valid_to is not None and interval.valid_to <= interval.valid_from:
            raise SecurityMasterError("symbol validity must be a non-empty half-open interval")
        if interval.confidence not in _CONFIDENCE_ORDER:
            raise SecurityMasterError(f"invalid mapping confidence: {interval.confidence}")
        if interval.ticker != normalize_ticker(interval.ticker):
            raise SecurityMasterError("symbol ticker is not normalized")
        if interval.exchange != normalize_exchange(interval.exchange):
            raise SecurityMasterError("symbol exchange is not normalized")
        if interval.scope == "live_edge_current" and interval.valid_from < interval.known_at.date():
            raise SecurityMasterError("current-state SEC evidence cannot backfill historical dates")
        symbol_rows.append(interval)
    symbol_rows.sort(
        key=lambda item: (
            item.exchange,
            item.ticker,
            item.valid_from,
            item.valid_to or date.max,
            item.security_id,
            item.source,
        )
    )

    issues: dict[str, MappingIssue] = {}
    for index, left in enumerate(symbol_rows):
        for right in symbol_rows[index + 1 :]:
            if left.exchange != right.exchange:
                continue
            overlap = _overlap(left, right)
            if overlap is None:
                continue
            if left.security_id == right.security_id and left.ticker != right.ticker:
                item = _issue(
                    "security_symbol_overlap",
                    left,
                    right,
                    overlap,
                    "one security has different simultaneous tickers on the same exchange",
                )
                issues[item.issue_id] = item
            elif left.security_id != right.security_id and left.ticker == right.ticker:
                item = _issue(
                    "ticker_collision",
                    left,
                    right,
                    overlap,
                    "one ticker/exchange/date maps to multiple securities",
                )
                issues[item.issue_id] = item
    return SecurityMaster(
        security_rows, tuple(symbol_rows), tuple(issues[key] for key in sorted(issues))
    )


SECURITIES_SCHEMA: Final = pa.schema(
    [
        ("security_id", pa.string()),
        ("cik", pa.int64()),
        ("class_title", pa.string()),
        ("security_type", pa.string()),
        ("domestic_flag", pa.bool_()),
        ("identity_anchor", pa.string()),
        ("source", pa.string()),
        ("evidence_pointer", pa.string()),
    ],
    metadata={b"usinv_table": b"securities", b"schema_version": b"1"},
)
SYMBOLS_SCHEMA: Final = pa.schema(
    [
        ("security_id", pa.string()),
        ("ticker", pa.string()),
        ("exchange", pa.string()),
        ("valid_from", pa.date32()),
        ("valid_to", pa.date32()),
        ("source", pa.string()),
        ("confidence", pa.string()),
        ("evidence_pointer", pa.string()),
        ("known_at", pa.timestamp("us", tz="UTC")),
        ("scope", pa.string()),
    ],
    metadata={b"usinv_table": b"security_symbols", b"schema_version": b"1"},
)
ISSUES_SCHEMA: Final = pa.schema(
    [
        ("issue_id", pa.string()),
        ("kind", pa.string()),
        ("ticker", pa.string()),
        ("exchange", pa.string()),
        ("valid_from", pa.date32()),
        ("valid_to", pa.date32()),
        ("security_ids", pa.list_(pa.field("element", pa.string()))),
        ("evidence_pointers", pa.list_(pa.field("element", pa.string()))),
        ("detail", pa.string()),
    ],
    metadata={b"usinv_table": b"security_mapping_issues", b"schema_version": b"1"},
)


@dataclass(frozen=True, slots=True)
class SecurityMasterSnapshot:
    snapshot_id: str
    output_dir: Path
    securities: int
    symbols: int
    issues: int
    from_cache: bool


def _jsonable(master: SecurityMaster) -> dict[str, object]:
    return {
        "version": master.version,
        "securities": [
            {
                "security_id": row.security_id,
                "cik": row.cik,
                "class_title": row.class_title,
                "security_type": row.security_type,
                "domestic_flag": row.domestic_flag,
                "identity_anchor": row.identity_anchor,
                "source": row.source,
                "evidence_pointer": row.evidence_pointer,
            }
            for row in master.securities
        ],
        "symbols": [
            {
                "security_id": row.security_id,
                "ticker": row.ticker,
                "exchange": row.exchange,
                "valid_from": row.valid_from.isoformat(),
                "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                "source": row.source,
                "confidence": row.confidence,
                "evidence_pointer": row.evidence_pointer,
                "known_at": row.known_at.astimezone(UTC).isoformat(),
                "scope": row.scope,
            }
            for row in master.symbols
        ],
        "issues": [
            {
                "issue_id": row.issue_id,
                "kind": row.kind,
                "ticker": row.ticker,
                "exchange": row.exchange,
                "valid_from": row.valid_from.isoformat(),
                "valid_to": row.valid_to.isoformat() if row.valid_to else None,
                "security_ids": row.security_ids,
                "evidence_pointers": row.evidence_pointers,
                "detail": row.detail,
            }
            for row in master.issues
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_snapshot(path: Path, snapshot_id: str) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityMasterError("security master snapshot manifest is unreadable") from exc
    if manifest.get("snapshot_id") != snapshot_id:
        raise SecurityMasterError("security master snapshot identity mismatch")
    for name, schema in (
        ("securities", SECURITIES_SCHEMA),
        ("security_symbols", SYMBOLS_SCHEMA),
        ("security_mapping_issues", ISSUES_SCHEMA),
    ):
        artifact = manifest.get("artifacts", {}).get(name, {})
        artifact_path = path / f"{name}.parquet"
        if (
            not artifact_path.is_file()
            or _sha256(artifact_path) != artifact.get("sha256")
            or not pq.ParquetFile(artifact_path).schema_arrow.equals(schema, check_metadata=True)
        ):
            raise SecurityMasterError(f"security master artifact failed verification: {name}")


def materialize_security_master(
    master: SecurityMaster,
    output_root: str | Path,
) -> SecurityMasterSnapshot:
    """Write an immutable content-addressed Parquet security-master snapshot."""
    payload = _jsonable(master)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    snapshot_id = hashlib.sha256(canonical).hexdigest()
    root = Path(output_root) / "snapshots"
    target = root / snapshot_id
    if target.exists():
        _verify_snapshot(target, snapshot_id)
        return SecurityMasterSnapshot(
            snapshot_id,
            target,
            len(master.securities),
            len(master.symbols),
            len(master.issues),
            True,
        )

    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        pq.write_table(
            pa.Table.from_pylist(payload["securities"], schema=SECURITIES_SCHEMA),
            temporary / "securities.parquet",
        )
        symbol_rows = [
            {
                **row,
                "valid_from": date.fromisoformat(row["valid_from"]),
                "valid_to": date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
                "known_at": datetime.fromisoformat(row["known_at"]),
            }
            for row in payload["symbols"]
        ]
        pq.write_table(
            pa.Table.from_pylist(symbol_rows, schema=SYMBOLS_SCHEMA),
            temporary / "security_symbols.parquet",
        )
        issue_rows = [
            {
                **row,
                "valid_from": date.fromisoformat(row["valid_from"]),
                "valid_to": date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            }
            for row in payload["issues"]
        ]
        pq.write_table(
            pa.Table.from_pylist(issue_rows, schema=ISSUES_SCHEMA),
            temporary / "security_mapping_issues.parquet",
        )
        artifacts = {}
        for name in ("securities", "security_symbols", "security_mapping_issues"):
            artifact_path = temporary / f"{name}.parquet"
            artifacts[name] = {
                "sha256": _sha256(artifact_path),
                "bytes": artifact_path.stat().st_size,
                "rows": pq.ParquetFile(artifact_path).metadata.num_rows,
            }
        manifest = {
            "schema_version": 1,
            "master_version": master.version,
            "snapshot_id": snapshot_id,
            "artifacts": artifacts,
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
    _verify_snapshot(target, snapshot_id)
    return SecurityMasterSnapshot(
        snapshot_id,
        target,
        len(master.securities),
        len(master.symbols),
        len(master.issues),
        False,
    )


def read_security_master_snapshot(path: str | Path) -> SecurityMaster:
    """Open a verified immutable security-master snapshot."""
    root = Path(path)
    snapshot_id = root.name
    if len(snapshot_id) != 64:
        raise SecurityMasterError("security master snapshot directory is not content-addressed")
    _verify_snapshot(root, snapshot_id)
    try:
        security_rows = pq.read_table(root / "securities.parquet").to_pylist()
        symbol_rows = pq.read_table(root / "security_symbols.parquet").to_pylist()
        issue_rows = pq.read_table(root / "security_mapping_issues.parquet").to_pylist()
        securities = tuple(Security(**row) for row in security_rows)
        symbols = tuple(SymbolInterval(**row) for row in symbol_rows)
    except (OSError, TypeError, ValueError) as exc:
        raise SecurityMasterError("security master snapshot rows are invalid") from exc
    master = build_security_master(securities, symbols)
    expected_issues = _jsonable(master)["issues"]
    normalized_issues = [
        {
            **row,
            "valid_from": row["valid_from"].isoformat(),
            "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
            "security_ids": tuple(row["security_ids"]),
            "evidence_pointers": tuple(row["evidence_pointers"]),
        }
        for row in issue_rows
    ]
    if normalized_issues != expected_issues:
        raise SecurityMasterError("security master issue reconstruction mismatch")
    canonical = json.dumps(_jsonable(master), sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != snapshot_id:
        raise SecurityMasterError("security master canonical identity mismatch")
    return master
