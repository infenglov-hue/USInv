"""Point-in-time corporate-action detection and cross-source reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Final, Literal

from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.data.prices.base import PriceConfigurationError, PricePayloadError

ActionType = Literal["split", "cash_dividend"]
AdjustmentQuality = Literal[
    "reconstructed",
    "vendor_frozen",
    "inferred_split",
    "unresolved",
]

SPLIT_RATIO_GRID: Final = tuple(
    Decimal(value)
    for value in (
        "4",
        "3",
        "2",
        "1.5",
        "0.5",
        "0.3333333333333333333333333333",
        "0.25",
        "0.2",
        "0.1",
        "0.05",
        "0.0333333333333333333333333333",
        "0.025",
        "0.02",
        "0.0125",
        "0.01",
    )
)
SPLIT_MATCH_TOLERANCE: Final = Decimal("0.02")
ACTION_VALUE_TOLERANCE: Final = Decimal("0.01")
OVERNIGHT_DISCONTINUITY: Final = Decimal("0.25")
VOLUME_CONFIRMATION_MULTIPLE: Final = Decimal("1.5")


def _finite_decimal(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise PricePayloadError(f"{label} must be an exact decimal")
    try:
        finite = value.is_finite()
    except InvalidOperation as exc:
        raise PricePayloadError(f"{label} is invalid") from exc
    if not finite or value <= 0:
        raise PricePayloadError(f"{label} must be finite and positive")
    return value


@dataclass(frozen=True, slots=True)
class CorporateActionObservation:
    """One immutable provider/detector claim before reconciliation."""

    security_id: str
    effective_session: date
    action_type: ActionType
    ratio_or_cash: Decimal
    currency: str | None
    source: str
    known_at: datetime
    evidence_pointer: str
    batch_id: str

    def __post_init__(self) -> None:
        if (
            not self.security_id
            or not self.source
            or not self.evidence_pointer
            or not self.batch_id
        ):
            raise PricePayloadError("corporate-action evidence identity is incomplete")
        if self.known_at.tzinfo is None:
            raise PricePayloadError("corporate-action known_at must be timezone-aware")
        try:
            default_calendar().session(self.effective_session)
        except CalendarError as exc:
            raise PricePayloadError(
                "corporate-action effective date must be an XNYS session"
            ) from exc
        _finite_decimal(self.ratio_or_cash, label="corporate-action value")
        if self.action_type == "split":
            if self.currency is not None:
                raise PricePayloadError("split observations cannot carry currency")
        elif self.action_type == "cash_dividend":
            if self.currency != "USD":
                raise PricePayloadError("cash dividends require explicit USD currency")
            if self.source == "detector":
                raise PricePayloadError("the price detector cannot declare cash dividends")
        else:
            raise PricePayloadError("unsupported corporate-action type")

    @property
    def key(self) -> tuple[str, date, ActionType]:
        return self.security_id, self.effective_session, self.action_type


@dataclass(frozen=True, slots=True)
class ReconciledCorporateAction:
    """One normalized event with explicit agreement or quarantine status."""

    security_id: str
    effective_session: date
    action_type: ActionType
    ratio_or_cash: Decimal | None
    currency: str | None
    known_at: datetime
    quality: AdjustmentQuality
    sources: tuple[str, ...]
    evidence_pointers: tuple[str, ...]
    batch_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if (
            not self.security_id
            or not self.sources
            or not self.evidence_pointers
            or not self.batch_ids
            or not self.reason
        ):
            raise PricePayloadError("reconciled corporate-action evidence is incomplete")
        if self.known_at.tzinfo is None:
            raise PricePayloadError("reconciled corporate-action known_at must be timezone-aware")
        try:
            default_calendar().session(self.effective_session)
        except CalendarError as exc:
            raise PricePayloadError(
                "reconciled corporate-action date must be an XNYS session"
            ) from exc
        if self.quality not in {
            "reconstructed",
            "vendor_frozen",
            "inferred_split",
            "unresolved",
        }:
            raise PricePayloadError("reconciled corporate-action quality is invalid")
        if self.quality == "unresolved":
            if self.ratio_or_cash is not None:
                raise PricePayloadError("unresolved corporate action cannot carry a trusted value")
        elif self.ratio_or_cash is None:
            raise PricePayloadError("resolved corporate action requires a trusted value")
        else:
            _finite_decimal(self.ratio_or_cash, label="reconciled corporate-action value")
        if self.action_type == "split" and self.currency is not None:
            raise PricePayloadError("reconciled split cannot carry currency")
        if self.action_type == "cash_dividend" and self.currency != "USD":
            raise PricePayloadError("reconciled cash dividend requires USD currency")
        if self.action_type not in {"split", "cash_dividend"}:
            raise PricePayloadError("reconciled corporate-action type is invalid")

    @property
    def usable_for_reconstruction(self) -> bool:
        return self.quality in {"reconstructed", "inferred_split"}


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    as_of: datetime
    actions: tuple[ReconciledCorporateAction, ...]
    quarantined_security_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise PriceConfigurationError("reconciliation as_of must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ActionDetectionPoint:
    """Raw plus independently adjusted evidence for one security/session."""

    security_id: str
    session: date
    raw_close: Decimal
    adjusted_close: Decimal
    volume: int
    raw_evidence_pointer: str
    adjusted_evidence_pointer: str
    batch_id: str

    def __post_init__(self) -> None:
        if (
            not self.security_id
            or not self.raw_evidence_pointer
            or not self.adjusted_evidence_pointer
            or not self.batch_id
        ):
            raise PricePayloadError("action-detector evidence is incomplete")
        _finite_decimal(self.raw_close, label="detector raw close")
        _finite_decimal(self.adjusted_close, label="detector adjusted close")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume <= 0:
            raise PricePayloadError("detector volume must be a positive integer")
        try:
            default_calendar().session(self.session)
        except CalendarError as exc:
            raise PricePayloadError("detector point is not an XNYS session") from exc


def _relative_difference(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right) / max(abs(left), abs(right))


def _matched_split_ratio(observed: Decimal) -> Decimal | None:
    matches = [
        candidate
        for candidate in SPLIT_RATIO_GRID
        if abs(observed - candidate) / candidate <= SPLIT_MATCH_TOLERANCE
    ]
    if not matches:
        return None
    return min(matches, key=lambda candidate: abs(observed - candidate) / candidate)


def _median_volume(points: list[ActionDetectionPoint]) -> Decimal:
    values = sorted(point.volume for point in points)
    midpoint = len(values) // 2
    if len(values) % 2:
        return Decimal(values[midpoint])
    return Decimal(values[midpoint - 1] + values[midpoint]) / Decimal(2)


def detect_split_candidates(
    points: tuple[ActionDetectionPoint, ...],
    *,
    earnings_8k_events: frozenset[tuple[str, date]] = frozenset(),
    calendar: XNYSCalendar | None = None,
) -> tuple[CorporateActionObservation, ...]:
    """Detect only corroborated rational split jumps; never invent cash dividends."""
    if not points:
        return ()
    session_calendar = calendar or default_calendar()
    ordered = tuple(sorted(points, key=lambda item: (item.security_id, item.session)))
    if len({(point.security_id, point.session) for point in ordered}) != len(ordered):
        raise PricePayloadError("action detector received duplicate security/session points")

    output: list[CorporateActionObservation] = []
    by_security: dict[str, list[ActionDetectionPoint]] = {}
    for point in ordered:
        by_security.setdefault(point.security_id, []).append(point)
    for security_id, series in sorted(by_security.items()):
        for index, (previous, current) in enumerate(pairwise(series), start=1):
            if session_calendar.previous_session(current.session).label != previous.session:
                continue
            raw_ratio = current.raw_close / previous.raw_close
            if abs(raw_ratio - Decimal(1)) <= OVERNIGHT_DISCONTINUITY:
                continue
            if (security_id, current.session) in earnings_8k_events:
                continue
            split_ratio = _matched_split_ratio(previous.raw_close / current.raw_close)
            if split_ratio is None:
                continue
            adjusted_ratio = current.adjusted_close / previous.adjusted_close
            if (
                abs(adjusted_ratio - Decimal(1)) > OVERNIGHT_DISCONTINUITY
                or abs(raw_ratio - adjusted_ratio) <= OVERNIGHT_DISCONTINUITY
            ):
                continue
            volume_baseline = _median_volume(series[:index])
            if Decimal(current.volume) / volume_baseline < VOLUME_CONFIRMATION_MULTIPLE:
                continue
            known_at = session_calendar.session(current.session).close_at.astimezone(UTC)
            output.append(
                CorporateActionObservation(
                    security_id,
                    current.session,
                    "split",
                    split_ratio,
                    None,
                    "detector",
                    known_at,
                    f"{previous.raw_evidence_pointer}|{current.raw_evidence_pointer}|"
                    f"{previous.adjusted_evidence_pointer}|{current.adjusted_evidence_pointer}",
                    current.batch_id,
                )
            )
    return tuple(output)


def _reconcile_group(
    observations: tuple[CorporateActionObservation, ...],
) -> ReconciledCorporateAction:
    first = observations[0]
    sources = tuple(sorted({item.source for item in observations}))
    pointers = tuple(sorted({item.evidence_pointer for item in observations}))
    batches = tuple(sorted({item.batch_id for item in observations}))
    known_at = max(item.known_at for item in observations).astimezone(UTC)
    currencies = {item.currency for item in observations}
    per_source_values = {
        source: {item.ratio_or_cash for item in observations if item.source == source}
        for source in sources
    }
    conflict = len(currencies) != 1 or any(
        len(values) != 1 for values in per_source_values.values()
    )
    values = tuple(item.ratio_or_cash for item in observations)
    if not conflict:
        conflict = any(
            _relative_difference(values[0], value) > ACTION_VALUE_TOLERANCE for value in values[1:]
        )
    if conflict:
        quality: AdjustmentQuality = "unresolved"
        value: Decimal | None = None
        reason = "sources disagree on currency or event value"
    else:
        value = sum(values, Decimal(0)) / Decimal(len(values))
        if first.action_type == "split" and sources == ("detector",):
            quality = "inferred_split"
            reason = "rational price discontinuity inferred without a declared source"
        elif len(sources) >= 2 and (
            first.action_type == "cash_dividend" or any(source != "detector" for source in sources)
        ):
            quality = "reconstructed"
            reason = "independent sources agree"
        else:
            quality = "unresolved"
            value = None
            reason = "only one declared source is available"
    return ReconciledCorporateAction(
        first.security_id,
        first.effective_session,
        first.action_type,
        value,
        next(iter(currencies)) if len(currencies) == 1 else None,
        known_at,
        quality,
        sources,
        pointers,
        batches,
        reason,
    )


def reconcile_corporate_actions(
    observations: tuple[CorporateActionObservation, ...],
    *,
    as_of: datetime,
) -> ReconciliationReport:
    """Reconcile only evidence already known at *as_of*; later rows are invisible."""
    if as_of.tzinfo is None:
        raise PriceConfigurationError("reconciliation as_of must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    visible = tuple(item for item in observations if item.known_at.astimezone(UTC) <= cutoff)
    groups: dict[tuple[str, date, ActionType], list[CorporateActionObservation]] = {}
    for observation in visible:
        groups.setdefault(observation.key, []).append(observation)
    actions = tuple(
        _reconcile_group(
            tuple(sorted(group, key=lambda item: (item.source, item.evidence_pointer)))
        )
        for _, group in sorted(groups.items())
    )
    quarantined = tuple(
        sorted({action.security_id for action in actions if action.quality == "unresolved"})
    )
    return ReconciliationReport(cutoff, actions, quarantined)
