"""Deterministic, resumable Alpaca-sized price batches for the Phase 2.3 universe."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Final

from usinv.calendar import EXCHANGE_TIMEZONE, CalendarError, default_calendar
from usinv.data.edgar.cover_shards import CoverEvidenceSnapshot, read_cover_evidence_snapshot
from usinv.data.edgar.securities import SecurityMasterError, normalize_exchange, normalize_ticker
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan
from usinv.data.prices.base import (
    PriceDataError,
    PriceProvider,
    PriceQuery,
    PriceSnapshot,
    materialize_price_snapshot,
    price_bindings_from_security_master,
    read_price_snapshot,
)

PRICE_UNIVERSE_VERSION: Final = "usinv-price-universe-v1"


class PriceUniverseError(PriceDataError):
    """Raised when a universe price plan or its exact batch set is invalid."""


@dataclass(frozen=True, slots=True)
class PriceUniverseTarget:
    security_id: str
    ticker: str
    exchange: str

    def __post_init__(self) -> None:
        try:
            ticker = normalize_ticker(self.ticker)
            exchange = normalize_exchange(self.exchange)
        except SecurityMasterError as exc:
            raise PriceUniverseError("price-universe target symbol is invalid") from exc
        if not self.security_id or ticker != self.ticker or exchange != self.exchange:
            raise PriceUniverseError("price-universe target identity is invalid")


@dataclass(frozen=True, slots=True)
class PriceUniversePlan:
    discovery_snapshot_id: str
    cover_snapshot_id: str
    signal_at: datetime
    start_at: datetime
    batch_size: int
    targets: tuple[PriceUniverseTarget, ...]
    version: str = PRICE_UNIVERSE_VERSION

    def __post_init__(self) -> None:
        if (
            len(self.discovery_snapshot_id) != 64
            or len(self.cover_snapshot_id) != 64
            or self.signal_at.tzinfo is None
            or self.start_at.tzinfo is None
            or self.start_at >= self.signal_at
            or self.batch_size <= 0
            or not self.targets
            or tuple(sorted(self.targets, key=lambda row: (row.ticker, row.security_id)))
            != self.targets
            or len({row.security_id for row in self.targets}) != len(self.targets)
            or len({row.ticker for row in self.targets}) != len(self.targets)
        ):
            raise PriceUniverseError("price-universe plan is invalid")

    @property
    def batches(self) -> tuple[tuple[PriceUniverseTarget, ...], ...]:
        return tuple(
            self.targets[index : index + self.batch_size]
            for index in range(0, len(self.targets), self.batch_size)
        )

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(_canonical(_plan_payload(self))).hexdigest()


@dataclass(frozen=True, slots=True)
class PriceUniverseSnapshot:
    snapshot_id: str
    output_dir: Path
    plan: PriceUniversePlan
    price_snapshots: tuple[PriceSnapshot, ...]
    from_cache: bool


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _plan_payload(plan: PriceUniversePlan) -> dict[str, object]:
    return {
        "version": plan.version,
        "discovery_snapshot_id": plan.discovery_snapshot_id,
        "cover_snapshot_id": plan.cover_snapshot_id,
        "signal_at": plan.signal_at.astimezone(UTC).isoformat(),
        "start_at": plan.start_at.astimezone(UTC).isoformat(),
        "batch_size": plan.batch_size,
        "targets": [
            {
                "security_id": row.security_id,
                "ticker": row.ticker,
                "exchange": row.exchange,
            }
            for row in plan.targets
        ],
    }


def build_price_universe_plan(
    discovery: FilingDiscoveryPlan,
    cover_snapshot: CoverEvidenceSnapshot,
    *,
    signal_at: datetime,
    window_sessions: int = 21,
    batch_size: int = 100,
) -> PriceUniversePlan:
    """Resolve active discovery rows to stable IDs before any price request is emitted."""
    if signal_at.tzinfo is None or window_sessions <= 0 or batch_size <= 0:
        raise PriceUniverseError("price-universe request bounds are invalid")
    verified_cover = read_cover_evidence_snapshot(cover_snapshot.output_dir)
    cutoff = signal_at.astimezone(UTC)
    if (
        verified_cover.snapshot_id != cover_snapshot.snapshot_id
        or verified_cover.merge.as_of.astimezone(UTC) != cutoff
        or discovery.listing_as_of != signal_at.date()
    ):
        raise PriceUniverseError("price-universe source identities or cutoff differ")
    calendar = default_calendar()
    try:
        session = calendar.session(signal_at.date())
        if session.close_at.astimezone(UTC) != cutoff:
            raise PriceUniverseError("price-universe signal is not the official XNYS close")
        first = session.label
        for _ in range(window_sessions - 1):
            first = calendar.previous_session(first).label
    except CalendarError as exc:
        raise PriceUniverseError("price-universe session window is invalid") from exc
    start_at = datetime.combine(first, time.min, EXCHANGE_TIMEZONE).astimezone(UTC)

    master = verified_cover.merge.master
    targets: dict[str, PriceUniverseTarget] = {}
    tickers: dict[str, str] = {}
    for row in discovery.rows:
        if (
            row.status != "discovered"
            or row.normalized_exchange is None
            or row.asset_type.casefold() != "stock"
        ):
            continue
        try:
            mapping = master.resolve(
                row.ticker,
                row.normalized_exchange,
                signal_at.date(),
                minimum_confidence="high",
            )
        except SecurityMasterError:
            continue
        if mapping.status != "mapped" or mapping.security_id is None:
            continue
        target = PriceUniverseTarget(mapping.security_id, row.ticker, row.normalized_exchange)
        prior = targets.get(target.security_id)
        if prior is not None and prior != target:
            raise PriceUniverseError("one security resolved to multiple active price targets")
        prior_security = tickers.get(target.ticker)
        if prior_security is not None and prior_security != target.security_id:
            raise PriceUniverseError("one provider ticker resolved to multiple active securities")
        targets[target.security_id] = target
        tickers[target.ticker] = target.security_id
    ordered = tuple(sorted(targets.values(), key=lambda row: (row.ticker, row.security_id)))
    return PriceUniversePlan(
        discovery.snapshot_id,
        verified_cover.snapshot_id,
        cutoff,
        start_at,
        batch_size,
        ordered,
    )


def _run_payload(
    plan: PriceUniversePlan,
    snapshots: tuple[PriceSnapshot, ...],
) -> dict[str, object]:
    return {
        "version": PRICE_UNIVERSE_VERSION,
        "plan": _plan_payload(plan),
        "plan_snapshot_id": plan.snapshot_id,
        "price_snapshot_ids": [row.snapshot_id for row in snapshots],
    }


def read_price_universe_snapshot(path: str | Path) -> PriceUniverseSnapshot:
    """Reopen an exact price-batch set and verify every referenced source snapshot."""
    root = Path(path)
    try:
        payload = json.loads((root / "price-universe.json").read_text(encoding="utf-8"))
        plan_payload = payload["plan"]
        plan = PriceUniversePlan(
            plan_payload["discovery_snapshot_id"],
            plan_payload["cover_snapshot_id"],
            datetime.fromisoformat(plan_payload["signal_at"]),
            datetime.fromisoformat(plan_payload["start_at"]),
            plan_payload["batch_size"],
            tuple(PriceUniverseTarget(**row) for row in plan_payload["targets"]),
            plan_payload["version"],
        )
        snapshot_ids = tuple(payload["price_snapshot_ids"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PriceUniverseError("price-universe artifact is invalid") from exc
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    price_root = root.parent.parent / "snapshots"
    if (
        root.name != snapshot_id
        or payload.get("version") != PRICE_UNIVERSE_VERSION
        or payload.get("plan_snapshot_id") != plan.snapshot_id
        or len(snapshot_ids) != len(plan.batches)
        or len(set(snapshot_ids)) != len(snapshot_ids)
        or any(not isinstance(value, str) or len(value) != 64 for value in snapshot_ids)
    ):
        raise PriceUniverseError("price-universe artifact identity is invalid")
    snapshots = tuple(read_price_snapshot(price_root / value) for value in snapshot_ids)
    return PriceUniverseSnapshot(snapshot_id, root, plan, snapshots, True)


def acquire_price_universe(
    provider: PriceProvider,
    plan: PriceUniversePlan,
    cover_snapshot: CoverEvidenceSnapshot,
    output_root: str | Path,
) -> PriceUniverseSnapshot:
    """Fetch and persist every exact target batch, then seal their complete manifest."""
    verified_cover = read_cover_evidence_snapshot(cover_snapshot.output_dir)
    if verified_cover.snapshot_id != plan.cover_snapshot_id:
        raise PriceUniverseError("price-universe plan uses a different security master")
    output = Path(output_root)
    snapshots: list[PriceSnapshot] = []
    for batch in plan.batches:
        symbols = tuple(row.ticker for row in batch)
        bindings = price_bindings_from_security_master(
            verified_cover.merge.master,
            symbols=symbols,
            start=plan.start_at.astimezone(EXCHANGE_TIMEZONE).date(),
            end=plan.signal_at.astimezone(EXCHANGE_TIMEZONE).date(),
        )
        raw = provider.fetch_daily_bars(PriceQuery(symbols, plan.start_at, plan.signal_at, "raw"))
        adjusted = provider.fetch_daily_bars(
            PriceQuery(symbols, plan.start_at, plan.signal_at, "all")
        )
        snapshots.append(materialize_price_snapshot(raw, adjusted, bindings, output))
    ordered = tuple(snapshots)
    payload = _run_payload(plan, ordered)
    snapshot_id = hashlib.sha256(_canonical(payload)).hexdigest()
    root = output / "universe-runs"
    target = root / snapshot_id
    if target.exists():
        existing = read_price_universe_snapshot(target)
        if existing.plan != plan:
            raise PriceUniverseError("price-universe cache conflicts with the input plan")
        return existing
    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        (temporary / "price-universe.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        root.mkdir(parents=True, exist_ok=True)
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    created = read_price_universe_snapshot(target)
    return PriceUniverseSnapshot(
        created.snapshot_id,
        created.output_dir,
        created.plan,
        created.price_snapshots,
        False,
    )
