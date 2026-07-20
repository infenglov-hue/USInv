"""As-of-anchored split and total-return factors that never mutate raw prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.data.prices.actions import ReconciledCorporateAction
from usinv.data.prices.base import PriceConfigurationError, PricePayloadError

EvidenceMode = Literal["research", "audit"]
FactorConsumer = Literal["stop", "total_return"]


class AdjustmentError(PricePayloadError):
    """Raised when event-time-safe factors cannot be constructed."""


class UnresolvedAdjustmentError(AdjustmentError):
    """Raised when an unresolved event crosses a forbidden consumer interval."""


@dataclass(frozen=True, slots=True)
class RawClose:
    security_id: str
    session: date
    close: Decimal
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.security_id or not self.evidence_pointer:
            raise PricePayloadError("raw-close evidence identity is incomplete")
        if not isinstance(self.close, Decimal) or not self.close.is_finite() or self.close <= 0:
            raise PricePayloadError("raw close must be a finite positive exact decimal")
        try:
            default_calendar().session(self.session)
        except CalendarError as exc:
            raise PricePayloadError("raw close is not an XNYS session") from exc


@dataclass(frozen=True, slots=True)
class PriceFactor:
    security_id: str
    session: date
    anchor_session: date
    split_factor: Decimal | None
    tr_factor: Decimal | None
    adjustment_quality: str
    action_refs: tuple[str, ...]

    def split_continuous(self, raw_close: Decimal) -> Decimal:
        if self.split_factor is None:
            raise UnresolvedAdjustmentError("split-continuous price crosses unresolved action")
        return raw_close * self.split_factor

    def total_return(self, raw_close: Decimal) -> Decimal:
        if self.tr_factor is None:
            raise UnresolvedAdjustmentError("total-return price crosses unresolved action")
        return raw_close * self.tr_factor


def raw_price_for_level_rule(raw: RawClose) -> Decimal:
    """The only admissible price for floors, ADV, market cap and LOO reference."""
    return raw.close


def _validate_inputs(
    raw_closes: tuple[RawClose, ...],
    *,
    anchor_session: date,
    as_of: datetime,
    evidence_mode: EvidenceMode,
    consumer: FactorConsumer,
    calendar: XNYSCalendar,
) -> tuple[str, tuple[RawClose, ...]]:
    if as_of.tzinfo is None:
        raise PriceConfigurationError("factor as_of must be timezone-aware")
    if evidence_mode not in {"research", "audit"}:
        raise PriceConfigurationError("evidence mode must be research or audit")
    if consumer not in {"stop", "total_return"}:
        raise PriceConfigurationError("factor consumer must be stop or total_return")
    if not raw_closes:
        raise PriceConfigurationError("at least one raw close is required")
    ordered = tuple(sorted(raw_closes, key=lambda item: item.session))
    security_ids = {item.security_id for item in ordered}
    if len(security_ids) != 1:
        raise PriceConfigurationError("one factor build may contain only one security")
    if len({item.session for item in ordered}) != len(ordered):
        raise PricePayloadError("raw closes contain a duplicate session")
    anchor = calendar.session(anchor_session)
    if anchor.close_at.astimezone(UTC) > as_of.astimezone(UTC):
        raise PriceConfigurationError("factor anchor session is newer than as_of")
    if ordered[-1].session > anchor_session:
        raise PriceConfigurationError("raw closes extend beyond factor anchor")
    return next(iter(security_ids)), ordered


def build_price_factors(
    raw_closes: tuple[RawClose, ...],
    actions: tuple[ReconciledCorporateAction, ...],
    *,
    anchor_session: date,
    as_of: datetime,
    evidence_mode: EvidenceMode,
    consumer: FactorConsumer,
    calendar: XNYSCalendar | None = None,
) -> tuple[PriceFactor, ...]:
    """Build backward factors using only actions effective and known by the anchor."""
    session_calendar = calendar or default_calendar()
    security_id, ordered = _validate_inputs(
        raw_closes,
        anchor_session=anchor_session,
        as_of=as_of,
        evidence_mode=evidence_mode,
        consumer=consumer,
        calendar=session_calendar,
    )
    cutoff = as_of.astimezone(UTC)
    applicable = tuple(
        sorted(
            (
                action
                for action in actions
                if action.security_id == security_id
                and action.effective_session <= anchor_session
                and action.known_at.astimezone(UTC) <= cutoff
            ),
            key=lambda action: (action.effective_session, action.action_type, action.sources),
        )
    )
    if evidence_mode == "audit" and any(
        action.quality in {"unresolved", "inferred_split", "vendor_frozen"} for action in applicable
    ):
        raise UnresolvedAdjustmentError(
            "audit mode rejects unresolved, inferred or vendor-frozen adjustments"
        )
    if consumer == "stop" and any(action.quality == "unresolved" for action in applicable):
        raise UnresolvedAdjustmentError("stop calculation rejects unresolved adjustments")

    closes = {item.session: item.close for item in ordered}
    dividend_factors: dict[tuple[date, tuple[str, ...]], Decimal] = {}
    for action in applicable:
        if action.quality == "unresolved" or action.action_type != "cash_dividend":
            continue
        if action.ratio_or_cash is None:
            raise AdjustmentError("resolved dividend is missing its cash value")
        previous_session = session_calendar.previous_session(action.effective_session).label
        previous_close = closes.get(previous_session)
        if previous_close is None or previous_close <= action.ratio_or_cash:
            raise UnresolvedAdjustmentError(
                "dividend adjustment requires a valid previous-session raw close"
            )
        dividend_factors[(action.effective_session, action.evidence_pointers)] = (
            previous_close - action.ratio_or_cash
        ) / previous_close

    output: list[PriceFactor] = []
    for raw in ordered:
        split_factor = Decimal(1)
        tr_factor = Decimal(1)
        refs: set[str] = set()
        quality = "reconstructed"
        unresolved = False
        for action in applicable:
            if not raw.session < action.effective_session <= anchor_session:
                continue
            refs.update(action.evidence_pointers)
            if action.quality == "unresolved":
                unresolved = True
                quality = "unresolved"
                continue
            if action.quality == "vendor_frozen":
                quality = "vendor_frozen"
            elif action.quality == "inferred_split" and quality == "reconstructed":
                quality = "inferred_split"
            if action.ratio_or_cash is None:
                raise AdjustmentError("resolved action is missing its event value")
            if action.action_type == "split":
                multiplier = Decimal(1) / action.ratio_or_cash
                split_factor *= multiplier
                tr_factor *= multiplier
            elif action.action_type == "cash_dividend":
                tr_factor *= dividend_factors[(action.effective_session, action.evidence_pointers)]
        output.append(
            PriceFactor(
                security_id,
                raw.session,
                anchor_session,
                None if unresolved else split_factor,
                None if unresolved else tr_factor,
                quality,
                tuple(sorted(refs)),
            )
        )
    return tuple(output)
