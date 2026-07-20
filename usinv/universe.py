"""Point-in-time universe construction with a persisted per-filter audit trail."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.config import UniverseConfig
from usinv.data.edgar.applicability import ApplicabilityCoverageReport, UniverseCandidate
from usinv.data.edgar.securities import Security, SecurityMaster, SecurityMasterError
from usinv.data.listings import AlphaListingRow, AlphaListingSnapshot
from usinv.scoring.sectors import SectorClassification, SectorMappingError, classify_sic

UNIVERSE_VERSION: Final = "usinv-universe-v1"
HYGIENE_STUB_VERSION: Final = "phase-2.3-pass-through-v1"
FPI_FORMS: Final = frozenset({"20-F", "6-K", "F-1"})
PRE_REVENUE_BIOTECH_SICS: Final = frozenset({2834, 2836, 8731})
SizeBucket = Literal["core", "large_cap"]


class UniverseError(ValueError):
    """Raised when universe evidence violates identity, time or filter contracts."""


class UniverseGateError(UniverseError):
    """Raised when Phase 2.3 acceptance would otherwise silently weaken."""


class UniverseStoreError(UniverseError):
    """Raised when an immutable universe snapshot is incomplete or corrupted."""


@dataclass(frozen=True, slots=True)
class UniversePriceBar:
    session: date
    raw_close: Decimal
    volume: int
    evidence_pointer: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.raw_close, Decimal)
            or not self.raw_close.is_finite()
            or self.raw_close <= 0
        ):
            raise UniverseError("universe raw close must be a positive exact decimal")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume <= 0:
            raise UniverseError("universe volume must be a positive integer")
        if not self.evidence_pointer:
            raise UniverseError("universe price bar requires an evidence pointer")

    @property
    def dollar_volume(self) -> Decimal:
        return self.raw_close * self.volume


@dataclass(frozen=True, slots=True)
class FilingFormObservation:
    form: str
    accepted: datetime
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.form or self.accepted.tzinfo is None or not self.evidence_pointer:
            raise UniverseError("filing-form evidence is incomplete")


@dataclass(frozen=True, slots=True)
class SecurityUniverseEvidence:
    security_id: str
    price_bars: tuple[UniversePriceBar, ...]
    shares_outstanding: Decimal | None
    shares_available_from: datetime | None
    shares_evidence_pointer: str | None
    sic: int | None
    sic_available_from: datetime | None
    sic_evidence_pointer: str | None
    form_history: tuple[FilingFormObservation, ...] = ()
    form_history_complete: bool = False
    form_history_evidence_pointer: str | None = None
    pre_revenue_biotech: bool | None = None
    biotech_available_from: datetime | None = None
    biotech_evidence_pointer: str | None = None

    def __post_init__(self) -> None:
        if not self.security_id:
            raise UniverseError("universe evidence requires a security ID")
        if self.shares_outstanding is not None and (
            not isinstance(self.shares_outstanding, Decimal)
            or not self.shares_outstanding.is_finite()
            or self.shares_outstanding <= 0
        ):
            raise UniverseError("shares outstanding must be a positive exact decimal")
        shares_fields = (self.shares_available_from, self.shares_evidence_pointer)
        if self.shares_outstanding is None and any(value is not None for value in shares_fields):
            raise UniverseError("missing shares cannot carry partial evidence")
        if self.shares_outstanding is not None and (
            self.shares_available_from is None
            or self.shares_available_from.tzinfo is None
            or not self.shares_evidence_pointer
        ):
            raise UniverseError("shares outstanding requires PIT evidence")
        sic_fields = (self.sic_available_from, self.sic_evidence_pointer)
        if self.sic is None and any(value is not None for value in sic_fields):
            raise UniverseError("missing SIC cannot carry partial evidence")
        if self.sic is not None and (
            isinstance(self.sic, bool)
            or not isinstance(self.sic, int)
            or self.sic_available_from is None
            or self.sic_available_from.tzinfo is None
            or not self.sic_evidence_pointer
        ):
            raise UniverseError("SIC requires valid point-in-time evidence")
        if self.form_history_complete and not self.form_history_evidence_pointer:
            raise UniverseError("complete form history requires an evidence pointer")
        if not self.form_history_complete and self.form_history_evidence_pointer is not None:
            raise UniverseError("incomplete form history cannot be marked with a completion proof")
        biotech_fields = (self.biotech_available_from, self.biotech_evidence_pointer)
        if self.pre_revenue_biotech is None and any(value is not None for value in biotech_fields):
            raise UniverseError("missing biotech classification cannot carry partial evidence")
        if self.pre_revenue_biotech is not None and (
            self.biotech_available_from is None
            or self.biotech_available_from.tzinfo is None
            or not self.biotech_evidence_pointer
        ):
            raise UniverseError("biotech classification requires point-in-time evidence")


@dataclass(frozen=True, slots=True)
class UniverseSnapshotRow:
    snapshot_date: date
    signal_at: datetime
    ticker: str
    name: str
    raw_exchange: str
    exchange: str | None
    asset_type: str
    listing_source_sha256: str
    security_id: str | None
    cik: int | None
    mapping_status: str
    membership_pass: bool
    exchange_pass: bool
    asset_type_pass: bool
    mapping_pass: bool
    domestic_pass: bool
    common_stock_pass: bool
    fpi_pass: bool
    primary_line_pass: bool
    raw_close_pass: bool
    dollar_volume_pass: bool
    shares_pass: bool
    market_cap_pass: bool
    sector_pass: bool
    hygiene_pass: bool
    included: bool
    raw_close: Decimal | None
    median_dollar_volume: Decimal | None
    shares_outstanding: Decimal | None
    class_market_cap: Decimal | None
    issuer_market_cap: Decimal | None
    sic: int | None
    ff12_code: int | None
    ff12_label: str | None
    ff49_code: int | None
    ff49_label: str | None
    size_bucket: SizeBucket | None
    exclusion_reasons: tuple[str, ...]
    evidence_pointers: tuple[str, ...]
    hygiene_status: str = HYGIENE_STUB_VERSION


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    snapshot_date: date
    signal_at: datetime
    listing_snapshot_id: str
    security_master_snapshot_id: str
    config_hash: str
    rows: tuple[UniverseSnapshotRow, ...]
    version: str = UNIVERSE_VERSION

    def __post_init__(self) -> None:
        if self.signal_at.tzinfo is None:
            raise UniverseError("universe signal timestamp must be timezone-aware")
        if self.snapshot_date != self.signal_at.date():
            raise UniverseError("universe snapshot date must match the signal timestamp date")
        if not self.listing_snapshot_id or len(self.security_master_snapshot_id) != 64:
            raise UniverseError("universe snapshot input identities are incomplete")
        if len(self.config_hash) != 64:
            raise UniverseError("universe configuration hash is invalid")
        if not self.rows:
            raise UniverseError("universe snapshot cannot be empty")

    @property
    def included(self) -> tuple[UniverseSnapshotRow, ...]:
        return tuple(row for row in self.rows if row.included)

    @property
    def identity_mapping_gaps(self) -> tuple[UniverseSnapshotRow, ...]:
        return tuple(
            row
            for row in self.rows
            if row.membership_pass
            and row.exchange_pass
            and row.asset_type_pass
            and not row.mapping_pass
        )

    @property
    def identity_mapping_rate(self) -> float:
        candidates = [
            row
            for row in self.rows
            if row.membership_pass and row.exchange_pass and row.asset_type_pass
        ]
        if not candidates:
            return 0.0
        return sum(row.mapping_pass for row in candidates) / len(candidates)

    @property
    def sector_mapping_gaps(self) -> tuple[UniverseSnapshotRow, ...]:
        return tuple(
            row
            for row in self.rows
            if row.mapping_pass and (row.sic is None or row.ff49_code is None)
        )

    @property
    def snapshot_id(self) -> str:
        payload = _snapshot_payload(self)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def applicability_candidates(self) -> tuple[UniverseCandidate, ...]:
        return tuple(
            UniverseCandidate(
                row.ticker,
                row.exchange or row.raw_exchange,
                row.snapshot_date,
                market_cap=row.issuer_market_cap,
                eligible=True,
            )
            for row in self.included
        )


def _required_sessions(
    session: date,
    count: int,
    calendar: XNYSCalendar,
) -> tuple[date, ...]:
    if count <= 0:
        raise UniverseError("dollar-volume window must be positive")
    try:
        labels = [calendar.session(session).label]
        while len(labels) < count:
            labels.append(calendar.previous_session(labels[-1]).label)
    except CalendarError as exc:
        raise UniverseError("universe snapshot requires a valid XNYS signal session") from exc
    return tuple(reversed(labels))


def _market_metrics(
    item: SecurityUniverseEvidence,
    *,
    signal_at: datetime,
    required_sessions: tuple[date, ...],
) -> tuple[Decimal | None, Decimal | None, tuple[str, ...]]:
    if any(bar.session > required_sessions[-1] for bar in item.price_bars):
        raise UniverseError("future raw price evidence reached the universe builder")
    by_session: dict[date, UniversePriceBar] = {}
    for bar in item.price_bars:
        if bar.session in by_session:
            raise UniverseError("duplicate raw price evidence reached the universe builder")
        by_session[bar.session] = bar
    selected = [by_session.get(session) for session in required_sessions]
    if any(bar is None for bar in selected):
        return None, None, ()
    complete = tuple(bar for bar in selected if bar is not None)
    pointers = tuple(sorted({bar.evidence_pointer for bar in complete}))
    return complete[-1].raw_close, median(bar.dollar_volume for bar in complete), pointers


def _validate_pit_evidence(item: SecurityUniverseEvidence, signal_at: datetime) -> None:
    cutoff = signal_at.astimezone(UTC)
    instants = [
        item.shares_available_from,
        item.sic_available_from,
        item.biotech_available_from,
        *(form.accepted for form in item.form_history),
    ]
    if any(instant is not None and instant.astimezone(UTC) > cutoff for instant in instants):
        raise UniverseError("future filing evidence reached the universe builder")


def _reason(flag: bool, value: str, reasons: list[str]) -> None:
    if not flag:
        reasons.append(value)


@dataclass(slots=True)
class _WorkRow:
    listing: AlphaListingRow
    exchange: str | None
    security: Security | None
    evidence: SecurityUniverseEvidence | None
    mapping_status: str
    membership_pass: bool
    exchange_pass: bool
    asset_type_pass: bool
    mapping_pass: bool
    domestic_pass: bool
    common_stock_pass: bool
    fpi_pass: bool
    raw_close: Decimal | None
    median_dollar_volume: Decimal | None
    raw_close_pass: bool
    dollar_volume_pass: bool
    shares_pass: bool
    class_market_cap: Decimal | None
    sector: SectorClassification | None
    sector_pass: bool
    evidence_pointers: set[str]


def build_universe_snapshot(
    listing_snapshot: AlphaListingSnapshot,
    master: SecurityMaster,
    evidence: Iterable[SecurityUniverseEvidence],
    *,
    signal_at: datetime,
    config: UniverseConfig,
    config_hash: str,
    security_master_snapshot_id: str,
    calendar: XNYSCalendar | None = None,
) -> UniverseSnapshot:
    """Apply the v1 filters while preserving every candidate and failure reason."""
    if signal_at.tzinfo is None:
        raise UniverseError("universe signal timestamp must be timezone-aware")
    session = signal_at.date()
    if listing_snapshot.as_of != session:
        raise UniverseError("listing snapshot date must equal the universe signal date")
    if len(config_hash) != 64 or not security_master_snapshot_id:
        raise UniverseError("universe input manifest identities are invalid")
    calendar = calendar or default_calendar()
    required_sessions = _required_sessions(session, config.dollar_volume_window_sessions, calendar)
    expected_signal = calendar.session(session).close_at.astimezone(UTC)
    if signal_at.astimezone(UTC) != expected_signal:
        raise UniverseError("universe signal timestamp must equal the official XNYS close")
    evidence_by_security: dict[str, SecurityUniverseEvidence] = {}
    for item in evidence:
        if item.security_id in evidence_by_security:
            raise UniverseError("duplicate security universe evidence")
        _validate_pit_evidence(item, signal_at)
        evidence_by_security[item.security_id] = item
    securities = {security.security_id: security for security in master.securities}
    approved_exchanges: set[str] = set()
    for exchange in config.exchanges:
        try:
            approved_exchanges.add(_normalize_exchange(exchange))
        except SecurityMasterError as exc:
            raise UniverseError("universe configuration has an unsupported exchange") from exc

    work: list[_WorkRow] = []
    for listing in sorted(
        (row for row in listing_snapshot.rows if row.state == "active"),
        key=lambda row: (row.exchange, row.symbol, row.row_number),
    ):
        membership_pass = listing.status == "Active" and (
            listing.ipo_date is None or listing.ipo_date <= session
        )
        try:
            exchange = _normalize_exchange(listing.exchange)
        except SecurityMasterError:
            exchange = None
        exchange_pass = exchange in approved_exchanges
        asset_type_pass = listing.asset_type.casefold() == "stock"
        mapping_status = "not_attempted"
        security: Security | None = None
        mapping_pass = False
        pointers = {f"alpha-vantage://{listing.source_sha256}/{listing.row_number}"}
        if membership_pass and exchange_pass and asset_type_pass and exchange is not None:
            try:
                mapping = master.resolve(
                    listing.symbol,
                    exchange,
                    session,
                    minimum_confidence="high",
                    required_security_type="common_stock",
                )
            except SecurityMasterError:
                mapping_status = "invalid_symbol"
            else:
                mapping_status = mapping.status
                pointers.update(mapping.evidence_pointers)
                if mapping.status == "mapped" and mapping.security_id is not None:
                    security = securities[mapping.security_id]
                    mapping_pass = True
        item = evidence_by_security.get(security.security_id) if security else None
        if item is not None:
            raw_close, median_dollar_volume, price_pointers = _market_metrics(
                item,
                signal_at=signal_at,
                required_sessions=required_sessions,
            )
            pointers.update(price_pointers)
        else:
            raw_close = None
            median_dollar_volume = None
        domestic_pass = bool(security and security.domestic_flag)
        common_stock_pass = bool(security and security.security_type == "common_stock")
        fpi_pass = bool(
            item is not None
            and item.form_history_complete
            and not any(form.form.upper() in FPI_FORMS for form in item.form_history)
        )
        if item and item.form_history_evidence_pointer:
            pointers.add(item.form_history_evidence_pointer)
        raw_close_pass = bool(
            raw_close is not None and raw_close > Decimal(str(config.raw_close_min_exclusive))
        )
        dollar_volume_pass = bool(
            median_dollar_volume is not None
            and median_dollar_volume >= Decimal(str(config.median_dollar_volume_min))
        )
        shares_pass = bool(item is not None and item.shares_outstanding is not None)
        if item and item.shares_evidence_pointer:
            pointers.add(item.shares_evidence_pointer)
        class_market_cap = (
            raw_close * item.shares_outstanding
            if raw_close is not None and item is not None and item.shares_outstanding is not None
            else None
        )
        sector: SectorClassification | None = None
        sector_pass = False
        if item is not None and item.sic is not None:
            try:
                sector = classify_sic(item.sic)
            except SectorMappingError:
                sector = None
            if item.sic_evidence_pointer:
                pointers.add(item.sic_evidence_pointer)
            biotech_known = (
                item.sic not in PRE_REVENUE_BIOTECH_SICS or item.pre_revenue_biotech is not None
            )
            pre_revenue = item.pre_revenue_biotech is True
            sector_pass = bool(
                sector is not None
                and sector.ff49 is not None
                and sector.excluded_group not in config.excluded_groups
                and biotech_known
                and not pre_revenue
            )
            if item.biotech_evidence_pointer:
                pointers.add(item.biotech_evidence_pointer)
        work.append(
            _WorkRow(
                listing,
                exchange,
                security,
                item,
                mapping_status,
                membership_pass,
                exchange_pass,
                asset_type_pass,
                mapping_pass,
                domestic_pass,
                common_stock_pass,
                fpi_pass,
                raw_close,
                median_dollar_volume,
                raw_close_pass,
                dollar_volume_pass,
                shares_pass,
                class_market_cap,
                sector,
                sector_pass,
                pointers,
            )
        )

    duplicates = Counter(row.security.security_id for row in work if row.security is not None)
    if any(count > 1 for count in duplicates.values()):
        raise UniverseError("multiple active listing rows resolved to one security")

    by_cik: dict[int, list[_WorkRow]] = defaultdict(list)
    for row in work:
        if row.security is not None and row.asset_type_pass and row.common_stock_pass:
            by_cik[row.security.cik].append(row)
    issuer_market_caps: dict[int, Decimal | None] = {}
    primary_security: dict[int, str | None] = {}
    for cik, rows in by_cik.items():
        caps = [row.class_market_cap for row in rows]
        issuer_market_caps[cik] = (
            sum((value for value in caps if value is not None), Decimal(0))
            if caps and all(value is not None for value in caps)
            else None
        )
        liquid = [
            row
            for row in rows
            if row.common_stock_pass
            and row.fpi_pass
            and row.raw_close_pass
            and row.dollar_volume_pass
            and row.shares_pass
            and row.sector_pass
            and row.median_dollar_volume is not None
        ]
        primary_security[cik] = (
            min(
                liquid,
                key=lambda row: (
                    -row.median_dollar_volume,  # type: ignore[operator]
                    row.security.security_id if row.security else "",
                ),
            ).security.security_id
            if liquid
            else None
        )

    output_rows: list[UniverseSnapshotRow] = []
    core_min = Decimal(str(config.core_market_cap_min))
    core_max = Decimal(str(config.core_market_cap_max))
    large_min = Decimal(str(config.large_cap_min_exclusive))
    for row in work:
        cik = row.security.cik if row.security else None
        issuer_market_cap = issuer_market_caps.get(cik) if cik is not None else None
        primary_line_pass = bool(
            row.security is not None
            and primary_security.get(row.security.cik) == row.security.security_id
        )
        size_bucket: SizeBucket | None = None
        if issuer_market_cap is not None:
            if core_min <= issuer_market_cap <= core_max:
                size_bucket = "core"
            elif issuer_market_cap > large_min:
                size_bucket = "large_cap"
        market_cap_pass = size_bucket is not None
        hygiene_pass = True
        reasons: list[str] = []
        _reason(row.membership_pass, "not_active_on_snapshot_date", reasons)
        _reason(row.exchange_pass, "unsupported_exchange", reasons)
        _reason(row.asset_type_pass, "not_stock_asset_type", reasons)
        _reason(row.mapping_pass, f"identity_{row.mapping_status}", reasons)
        _reason(row.domestic_pass, "not_domestic", reasons)
        _reason(row.common_stock_pass, "not_common_stock", reasons)
        _reason(row.fpi_pass, "fpi_or_missing_form_evidence", reasons)
        _reason(primary_line_pass, "not_most_liquid_issuer_line", reasons)
        _reason(row.raw_close_pass, "raw_close_missing_or_not_above_floor", reasons)
        _reason(row.dollar_volume_pass, "dollar_volume_missing_or_below_floor", reasons)
        _reason(row.shares_pass, "shares_outstanding_missing", reasons)
        _reason(market_cap_pass, "market_cap_missing_or_below_floor", reasons)
        _reason(row.sector_pass, "sector_excluded_or_unresolved", reasons)
        _reason(hygiene_pass, "hygiene_gate_failed", reasons)
        included = not reasons
        output_rows.append(
            UniverseSnapshotRow(
                session,
                signal_at.astimezone(UTC),
                row.listing.symbol,
                row.listing.name,
                row.listing.exchange,
                row.exchange,
                row.listing.asset_type,
                row.listing.source_sha256,
                row.security.security_id if row.security else None,
                cik,
                row.mapping_status,
                row.membership_pass,
                row.exchange_pass,
                row.asset_type_pass,
                row.mapping_pass,
                row.domestic_pass,
                row.common_stock_pass,
                row.fpi_pass,
                primary_line_pass,
                row.raw_close_pass,
                row.dollar_volume_pass,
                row.shares_pass,
                market_cap_pass,
                row.sector_pass,
                hygiene_pass,
                included,
                row.raw_close,
                row.median_dollar_volume,
                row.evidence.shares_outstanding if row.evidence else None,
                row.class_market_cap,
                issuer_market_cap,
                row.evidence.sic if row.evidence else None,
                row.sector.ff12.code if row.sector else None,
                row.sector.ff12.label if row.sector else None,
                row.sector.ff49.code if row.sector and row.sector.ff49 else None,
                row.sector.ff49.label if row.sector and row.sector.ff49 else None,
                size_bucket,
                tuple(reasons),
                tuple(sorted(row.evidence_pointers)),
            )
        )
    output_rows.sort(key=lambda row: (row.raw_exchange, row.ticker, row.security_id or ""))
    return UniverseSnapshot(
        session,
        signal_at.astimezone(UTC),
        listing_snapshot.snapshot_id,
        security_master_snapshot_id,
        config_hash,
        tuple(output_rows),
    )


def _normalize_exchange(value: str) -> str:
    from usinv.data.edgar.securities import normalize_exchange

    return normalize_exchange(value)


def enforce_phase_2_3_gate(
    snapshot: UniverseSnapshot,
    coverage: ApplicabilityCoverageReport,
) -> None:
    """Block Phase 2.3 on identity, sector, mandatory-input or D030 coverage gaps."""
    if snapshot.identity_mapping_gaps:
        raise UniverseGateError(
            f"Phase 2.3 has {len(snapshot.identity_mapping_gaps)} unresolved identity mappings"
        )
    if snapshot.sector_mapping_gaps:
        raise UniverseGateError(
            f"Phase 2.3 has {len(snapshot.sector_mapping_gaps)} unresolved FF49 mappings"
        )
    if not snapshot.included:
        raise UniverseGateError("Phase 2.3 final universe is empty")
    if coverage.candidates != len(snapshot.included):
        raise UniverseGateError("applicability denominator does not match the final universe")
    if not coverage.passed:
        raise UniverseGateError(
            "Phase 2.3 applicability coverage, mandatory inputs or mappings failed"
        )


UNIVERSE_ROWS_SCHEMA: Final = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("signal_at", pa.timestamp("us", tz="UTC")),
        ("ticker", pa.string()),
        ("name", pa.string()),
        ("raw_exchange", pa.string()),
        ("exchange", pa.string()),
        ("asset_type", pa.string()),
        ("listing_source_sha256", pa.string()),
        ("security_id", pa.string()),
        ("cik", pa.int64()),
        ("mapping_status", pa.string()),
        ("membership_pass", pa.bool_()),
        ("exchange_pass", pa.bool_()),
        ("asset_type_pass", pa.bool_()),
        ("mapping_pass", pa.bool_()),
        ("domestic_pass", pa.bool_()),
        ("common_stock_pass", pa.bool_()),
        ("fpi_pass", pa.bool_()),
        ("primary_line_pass", pa.bool_()),
        ("raw_close_pass", pa.bool_()),
        ("dollar_volume_pass", pa.bool_()),
        ("shares_pass", pa.bool_()),
        ("market_cap_pass", pa.bool_()),
        ("sector_pass", pa.bool_()),
        ("hygiene_pass", pa.bool_()),
        ("included", pa.bool_()),
        ("raw_close_exact", pa.string()),
        ("median_dollar_volume_exact", pa.string()),
        ("shares_outstanding_exact", pa.string()),
        ("class_market_cap_exact", pa.string()),
        ("issuer_market_cap_exact", pa.string()),
        ("sic", pa.int64()),
        ("ff12_code", pa.int64()),
        ("ff12_label", pa.string()),
        ("ff49_code", pa.int64()),
        ("ff49_label", pa.string()),
        ("size_bucket", pa.string()),
        ("exclusion_reasons", pa.list_(pa.field("element", pa.string()))),
        ("evidence_pointers", pa.list_(pa.field("element", pa.string()))),
        ("hygiene_status", pa.string()),
    ],
    metadata={b"usinv_table": b"universe_snapshots", b"schema_version": b"1"},
)


@dataclass(frozen=True, slots=True)
class UniverseSnapshotArtifact:
    snapshot_id: str
    output_dir: Path
    candidates: int
    included: int
    from_cache: bool


def _exact(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _row_payload(row: UniverseSnapshotRow) -> dict[str, object]:
    return {
        "snapshot_date": row.snapshot_date.isoformat(),
        "signal_at": row.signal_at.astimezone(UTC).isoformat(),
        "ticker": row.ticker,
        "name": row.name,
        "raw_exchange": row.raw_exchange,
        "exchange": row.exchange,
        "asset_type": row.asset_type,
        "listing_source_sha256": row.listing_source_sha256,
        "security_id": row.security_id,
        "cik": row.cik,
        "mapping_status": row.mapping_status,
        "membership_pass": row.membership_pass,
        "exchange_pass": row.exchange_pass,
        "asset_type_pass": row.asset_type_pass,
        "mapping_pass": row.mapping_pass,
        "domestic_pass": row.domestic_pass,
        "common_stock_pass": row.common_stock_pass,
        "fpi_pass": row.fpi_pass,
        "primary_line_pass": row.primary_line_pass,
        "raw_close_pass": row.raw_close_pass,
        "dollar_volume_pass": row.dollar_volume_pass,
        "shares_pass": row.shares_pass,
        "market_cap_pass": row.market_cap_pass,
        "sector_pass": row.sector_pass,
        "hygiene_pass": row.hygiene_pass,
        "included": row.included,
        "raw_close_exact": _exact(row.raw_close),
        "median_dollar_volume_exact": _exact(row.median_dollar_volume),
        "shares_outstanding_exact": _exact(row.shares_outstanding),
        "class_market_cap_exact": _exact(row.class_market_cap),
        "issuer_market_cap_exact": _exact(row.issuer_market_cap),
        "sic": row.sic,
        "ff12_code": row.ff12_code,
        "ff12_label": row.ff12_label,
        "ff49_code": row.ff49_code,
        "ff49_label": row.ff49_label,
        "size_bucket": row.size_bucket,
        "exclusion_reasons": row.exclusion_reasons,
        "evidence_pointers": row.evidence_pointers,
        "hygiene_status": row.hygiene_status,
    }


def _snapshot_payload(snapshot: UniverseSnapshot) -> dict[str, object]:
    return {
        "version": snapshot.version,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "signal_at": snapshot.signal_at.astimezone(UTC).isoformat(),
        "listing_snapshot_id": snapshot.listing_snapshot_id,
        "security_master_snapshot_id": snapshot.security_master_snapshot_id,
        "config_hash": snapshot.config_hash,
        "rows": [_row_payload(row) for row in snapshot.rows],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_universe_artifact(path: Path, snapshot: UniverseSnapshot) -> None:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UniverseStoreError("universe snapshot manifest is unreadable") from exc
    parquet_path = path / "universe_snapshots.parquet"
    artifact = manifest.get("artifact", {})
    if (
        manifest.get("snapshot_id") != snapshot.snapshot_id
        or not parquet_path.is_file()
        or _sha256(parquet_path) != artifact.get("sha256")
        or not pq.ParquetFile(parquet_path).schema_arrow.equals(
            UNIVERSE_ROWS_SCHEMA, check_metadata=True
        )
    ):
        raise UniverseStoreError("universe snapshot failed verification")


def materialize_universe_snapshot(
    snapshot: UniverseSnapshot,
    output_root: str | Path,
) -> UniverseSnapshotArtifact:
    """Write an immutable Parquet audit table and exact input manifest."""
    root = Path(output_root) / "universe" / snapshot.snapshot_date.isoformat()
    target = root / snapshot.snapshot_id
    if target.exists():
        _verify_universe_artifact(target, snapshot)
        return UniverseSnapshotArtifact(
            snapshot.snapshot_id,
            target,
            len(snapshot.rows),
            len(snapshot.included),
            True,
        )
    temporary = root / f".{snapshot.snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        table = pa.Table.from_pylist(
            [
                {
                    **_row_payload(row),
                    "snapshot_date": row.snapshot_date,
                    "signal_at": row.signal_at.astimezone(UTC),
                }
                for row in snapshot.rows
            ],
            schema=UNIVERSE_ROWS_SCHEMA,
        )
        parquet_path = temporary / "universe_snapshots.parquet"
        pq.write_table(table, parquet_path, compression="zstd")
        exclusion_counts = Counter(
            reason for row in snapshot.rows for reason in row.exclusion_reasons
        )
        manifest = {
            "schema_version": 1,
            "version": snapshot.version,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_date": snapshot.snapshot_date.isoformat(),
            "signal_at": snapshot.signal_at.astimezone(UTC).isoformat(),
            "inputs": {
                "listing_snapshot_id": snapshot.listing_snapshot_id,
                "security_master_snapshot_id": snapshot.security_master_snapshot_id,
                "config_hash": snapshot.config_hash,
            },
            "counts": {
                "candidates": len(snapshot.rows),
                "included": len(snapshot.included),
                "identity_mapping_gaps": len(snapshot.identity_mapping_gaps),
                "identity_mapping_rate": snapshot.identity_mapping_rate,
                "sector_mapping_gaps": len(snapshot.sector_mapping_gaps),
                "exclusions": dict(sorted(exclusion_counts.items())),
            },
            "hygiene_status": HYGIENE_STUB_VERSION,
            "artifact": {
                "path": "universe_snapshots.parquet",
                "rows": len(snapshot.rows),
                "sha256": _sha256(parquet_path),
            },
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
    _verify_universe_artifact(target, snapshot)
    return UniverseSnapshotArtifact(
        snapshot.snapshot_id,
        target,
        len(snapshot.rows),
        len(snapshot.included),
        False,
    )


def read_universe_snapshot(path: str | Path) -> UniverseSnapshot:
    """Open and verify a materialized universe audit snapshot."""
    root = Path(path)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["schema_version"] != 1 or manifest["version"] != UNIVERSE_VERSION:
            raise ValueError("schema/version")
        inputs = manifest["inputs"]
        table_rows = pq.read_table(root / "universe_snapshots.parquet").to_pylist()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UniverseStoreError("universe snapshot artifact is invalid") from exc

    def decimal_value(value: str | None) -> Decimal | None:
        return Decimal(value) if value is not None else None

    try:
        rows = tuple(
            UniverseSnapshotRow(
                snapshot_date=row["snapshot_date"],
                signal_at=row["signal_at"],
                ticker=row["ticker"],
                name=row["name"],
                raw_exchange=row["raw_exchange"],
                exchange=row["exchange"],
                asset_type=row["asset_type"],
                listing_source_sha256=row["listing_source_sha256"],
                security_id=row["security_id"],
                cik=row["cik"],
                mapping_status=row["mapping_status"],
                membership_pass=row["membership_pass"],
                exchange_pass=row["exchange_pass"],
                asset_type_pass=row["asset_type_pass"],
                mapping_pass=row["mapping_pass"],
                domestic_pass=row["domestic_pass"],
                common_stock_pass=row["common_stock_pass"],
                fpi_pass=row["fpi_pass"],
                primary_line_pass=row["primary_line_pass"],
                raw_close_pass=row["raw_close_pass"],
                dollar_volume_pass=row["dollar_volume_pass"],
                shares_pass=row["shares_pass"],
                market_cap_pass=row["market_cap_pass"],
                sector_pass=row["sector_pass"],
                hygiene_pass=row["hygiene_pass"],
                included=row["included"],
                raw_close=decimal_value(row["raw_close_exact"]),
                median_dollar_volume=decimal_value(row["median_dollar_volume_exact"]),
                shares_outstanding=decimal_value(row["shares_outstanding_exact"]),
                class_market_cap=decimal_value(row["class_market_cap_exact"]),
                issuer_market_cap=decimal_value(row["issuer_market_cap_exact"]),
                sic=row["sic"],
                ff12_code=row["ff12_code"],
                ff12_label=row["ff12_label"],
                ff49_code=row["ff49_code"],
                ff49_label=row["ff49_label"],
                size_bucket=row["size_bucket"],
                exclusion_reasons=tuple(row["exclusion_reasons"]),
                evidence_pointers=tuple(row["evidence_pointers"]),
                hygiene_status=row["hygiene_status"],
            )
            for row in table_rows
        )
        snapshot = UniverseSnapshot(
            date.fromisoformat(manifest["snapshot_date"]),
            datetime.fromisoformat(manifest["signal_at"]),
            inputs["listing_snapshot_id"],
            inputs["security_master_snapshot_id"],
            inputs["config_hash"],
            rows,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UniverseStoreError("universe snapshot rows are invalid") from exc
    if manifest.get("snapshot_id") != snapshot.snapshot_id or root.name != snapshot.snapshot_id:
        raise UniverseStoreError("universe snapshot identity does not match its directory")
    _verify_universe_artifact(root, snapshot)
    return snapshot
