"""Canonical in-memory order, fill, position, cash and NAV ledger contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class LedgerError(ValueError):
    """Raised when a ledger record violates an accounting identity."""


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderReason(StrEnum):
    ENTRY = "entry"
    ROUTINE_EXIT = "routine_exit"
    THESIS_BREAK = "thesis_break"
    OTC_EXIT = "otc_exit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A close-time intent; the engine derives the LOO reference from market evidence."""

    order_id: str
    security_id: str
    ticker: str
    side: OrderSide
    quantity: Decimal
    signal_session: date
    reason: OrderReason
    collar_fraction: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.order_id or not self.security_id or not self.ticker:
            raise LedgerError("order request identity fields are required")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise LedgerError("order quantity must be finite and positive")
        if self.collar_fraction is not None and (
            not self.collar_fraction.is_finite()
            or not Decimal(0) < self.collar_fraction < Decimal(1)
        ):
            raise LedgerError("order collar must be in (0, 1)")


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    root_order_id: str
    security_id: str
    ticker: str
    side: OrderSide
    quantity: Decimal
    reason: OrderReason
    signal_session: date
    scheduled_session: date
    reference_close: Decimal
    limit_price: Decimal
    collar_fraction: Decimal
    attempt: int
    status: OrderStatus
    status_reason: str


@dataclass(frozen=True, slots=True)
class FillRecord:
    fill_id: str
    order_id: str
    security_id: str
    side: OrderSide
    session: date
    quantity: Decimal
    assumed_price: Decimal
    notional: Decimal
    cost: Decimal
    cash_delta: Decimal
    fill_quality: str
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class LedgerPosition:
    position_id: str
    security_id: str
    ticker: str
    quantity: Decimal
    cost_basis_per_share: Decimal
    high_water_mark: Decimal
    entry_session: date

    def __post_init__(self) -> None:
        if not self.position_id or not self.security_id or not self.ticker:
            raise LedgerError("position identity fields are required")
        values = (self.quantity, self.cost_basis_per_share, self.high_water_mark)
        if any(not value.is_finite() or value <= 0 for value in values):
            raise LedgerError("position quantity and price bases must be positive")


@dataclass(frozen=True, slots=True)
class CashSettlement:
    security_id: str
    trade_session: date
    settlement_session: date
    amount: Decimal
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class NavRecord:
    session: date
    settled_cash: Decimal
    unsettled_cash: Decimal
    market_value: Decimal
    nav: Decimal
    cumulative_costs: Decimal


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    security_id: str
    session: date
    action_type: str
    cash_delta: Decimal
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class TerminationRecord:
    security_id: str
    session: date
    kind: str
    quantity: Decimal
    primary_value: Decimal
    final_close_sensitivity_value: Decimal | None
    haircut_30_sensitivity_value: Decimal | None
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class RunIdentity:
    code_sha: str
    config_hash: str
    data_manifest_hash: str
    evidence_mode: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not all((self.code_sha, self.config_hash, self.data_manifest_hash)):
            raise LedgerError("run identity hashes are required")
        if self.evidence_mode not in {"research", "audit"}:
            raise LedgerError("evidence mode must be research or audit")
        if self.created_at.tzinfo is None:
            raise LedgerError("run creation timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    identity: RunIdentity
    settled_cash: Decimal
    unsettled_settlements: tuple[CashSettlement, ...]
    positions: tuple[LedgerPosition, ...]
    orders: tuple[OrderRecord, ...]
    fills: tuple[FillRecord, ...]
    actions: tuple[CorporateActionRecord, ...]
    terminations: tuple[TerminationRecord, ...]
    nav: tuple[NavRecord, ...]
