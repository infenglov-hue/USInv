"""Execution-faithful event loop shared by historical and forward workflows."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from usinv.backtest.costs import FixedBpsCostModel, money
from usinv.calendar import CalendarError, XNYSCalendar, default_calendar
from usinv.ledger import (
    CashSettlement,
    CorporateActionRecord,
    FillRecord,
    LedgerPosition,
    LedgerSnapshot,
    NavRecord,
    OrderReason,
    OrderRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    RunIdentity,
    TerminationRecord,
)


class BacktestError(ValueError):
    """Raised when simulation inputs cannot support an evidence-faithful run."""


class ManualExecutionRequired(BacktestError):
    """Raised after an exit fails its opening collar on four eligible opens."""


class TerminationEvidenceError(BacktestError):
    """Raised when a termination lacks the evidence needed for its classification."""


class TerminationKind(StrEnum):
    ACQUISITION = "acquisition"
    OTC_REMOVAL = "otc_removal"
    BANKRUPTCY = "bankruptcy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MarketBar:
    """Official close plus an opening-auction price or documented daily proxy."""

    security_id: str
    session: date
    official_close: Decimal | None
    open_price: Decimal | None
    close_available_from: datetime | None
    fill_quality: str
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.security_id or not self.evidence_pointer:
            raise BacktestError("market bar identity and evidence pointer are required")
        for value in (self.official_close, self.open_price):
            if value is not None and (not value.is_finite() or value <= 0):
                raise BacktestError("market prices must be finite and positive")
        if self.official_close is not None and (
            self.close_available_from is None or self.close_available_from.tzinfo is None
        ):
            raise BacktestError("official close requires timezone-aware availability")
        if self.fill_quality not in {"auction_verified", "daily_open_proxy"}:
            raise BacktestError("fill quality must be auction_verified or daily_open_proxy")


@dataclass(frozen=True, slots=True)
class SplitEvent:
    security_id: str
    effective_session: date
    new_for_old: Decimal
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.new_for_old.is_finite() or self.new_for_old <= 0:
            raise BacktestError("split ratio must be finite and positive")
        if not self.evidence_pointer:
            raise BacktestError("split evidence pointer is required")


@dataclass(frozen=True, slots=True)
class DividendEvent:
    security_id: str
    payment_session: date
    cash_per_share: Decimal
    evidence_pointer: str

    def __post_init__(self) -> None:
        if not self.cash_per_share.is_finite() or self.cash_per_share < 0:
            raise BacktestError("dividend must be finite and non-negative")
        if not self.evidence_pointer:
            raise BacktestError("dividend evidence pointer is required")


@dataclass(frozen=True, slots=True)
class TerminationEvent:
    security_id: str
    session: date
    kind: TerminationKind
    evidence_pointer: str
    cash_consideration_per_share: Decimal | None = None
    stock_consideration_value_per_share: Decimal | None = None
    documented_recovery_per_share: Decimal | None = None
    final_close: Decimal | None = None
    stock_consideration_requires_valuation: bool = False

    def __post_init__(self) -> None:
        if not self.security_id or not self.evidence_pointer:
            raise BacktestError("termination identity and evidence pointer are required")
        values = (
            self.cash_consideration_per_share,
            self.stock_consideration_value_per_share,
            self.documented_recovery_per_share,
            self.final_close,
        )
        if any(value is not None and (not value.is_finite() or value < 0) for value in values):
            raise BacktestError("termination values must be finite and non-negative")


@dataclass(slots=True)
class _PendingOrder:
    root_order_id: str
    security_id: str
    ticker: str
    side: OrderSide
    quantity: Decimal
    reason: OrderReason
    signal_session: date
    scheduled_session: date
    reference_close: Decimal
    collar_fraction: Decimal
    attempt: int

    @property
    def order_id(self) -> str:
        return self.root_order_id if self.attempt == 1 else f"{self.root_order_id}:R{self.attempt}"

    @property
    def limit_price(self) -> Decimal:
        direction = Decimal(1) if self.side is OrderSide.BUY else Decimal(-1)
        return self.reference_close * (Decimal(1) + direction * self.collar_fraction)


@dataclass(frozen=True, slots=True)
class BacktestResult:
    ledger: LedgerSnapshot

    @property
    def total_costs(self) -> Decimal:
        return money(sum((fill.cost for fill in self.ledger.fills), start=Decimal(0)))


class BacktestEngine:
    """Deterministic, settled-cash, LOO-collar simulation."""

    def __init__(
        self,
        *,
        identity: RunIdentity,
        initial_settled_cash: Decimal,
        initial_positions: tuple[LedgerPosition, ...] = (),
        cost_model: FixedBpsCostModel | None = None,
        calendar: XNYSCalendar | None = None,
    ) -> None:
        if not initial_settled_cash.is_finite() or initial_settled_cash < 0:
            raise BacktestError("initial settled cash must be finite and non-negative")
        if len({item.security_id for item in initial_positions}) != len(initial_positions):
            raise BacktestError("initial position security ids must be unique")
        self.identity = identity
        self.initial_settled_cash = money(initial_settled_cash)
        self.initial_positions = initial_positions
        self.cost_model = cost_model or FixedBpsCostModel()
        self.calendar = calendar or default_calendar()

    def _validate_bar(self, bar: MarketBar) -> None:
        try:
            session = self.calendar.session(bar.session)
        except CalendarError as exc:
            raise BacktestError("market bar is not an XNYS session") from exc
        if bar.close_available_from is not None and (
            bar.close_available_from.astimezone(UTC) != session.close_at.astimezone(UTC)
        ):
            raise BacktestError("official close availability must equal the session close")

    @staticmethod
    def _collar(request: OrderRequest) -> Decimal:
        if request.collar_fraction is not None:
            return request.collar_fraction
        if request.reason is OrderReason.THESIS_BREAK:
            return Decimal("0.10")
        return Decimal("0.05")

    def _pending_from_request(
        self,
        request: OrderRequest,
        *,
        reference_close: Decimal,
    ) -> _PendingOrder:
        return _PendingOrder(
            root_order_id=request.order_id,
            security_id=request.security_id,
            ticker=request.ticker,
            side=request.side,
            quantity=request.quantity,
            reason=request.reason,
            signal_session=request.signal_session,
            scheduled_session=self.calendar.next_session(request.signal_session).label,
            reference_close=reference_close,
            collar_fraction=self._collar(request),
            attempt=1,
        )

    @staticmethod
    def _order_record(
        order: _PendingOrder,
        status: OrderStatus,
        status_reason: str,
    ) -> OrderRecord:
        return OrderRecord(
            order_id=order.order_id,
            root_order_id=order.root_order_id,
            security_id=order.security_id,
            ticker=order.ticker,
            side=order.side,
            quantity=order.quantity,
            reason=order.reason,
            signal_session=order.signal_session,
            scheduled_session=order.scheduled_session,
            reference_close=order.reference_close,
            limit_price=order.limit_price,
            collar_fraction=order.collar_fraction,
            attempt=order.attempt,
            status=status,
            status_reason=status_reason,
        )

    def run(
        self,
        sessions: tuple[date, ...],
        bars: tuple[MarketBar, ...],
        *,
        order_requests: tuple[OrderRequest, ...] = (),
        splits: tuple[SplitEvent, ...] = (),
        dividends: tuple[DividendEvent, ...] = (),
        terminations: tuple[TerminationEvent, ...] = (),
    ) -> BacktestResult:
        """Run all events in official exchange-session order."""
        ordered_sessions = tuple(sorted(sessions))
        if not ordered_sessions or len(set(ordered_sessions)) != len(ordered_sessions):
            raise BacktestError("run sessions must be non-empty and unique")
        for session in ordered_sessions:
            self.calendar.session(session)
        for previous, current in pairwise(ordered_sessions):
            if self.calendar.next_session(previous).label != current:
                raise BacktestError("run sessions must contain every consecutive XNYS session")

        bar_index: dict[tuple[date, str], MarketBar] = {}
        for bar in bars:
            self._validate_bar(bar)
            key = (bar.session, bar.security_id)
            if key in bar_index:
                raise BacktestError("market bars contain duplicate session/security rows")
            bar_index[key] = bar

        request_ids = {request.order_id for request in order_requests}
        if len(request_ids) != len(order_requests):
            raise BacktestError("order request ids must be unique")
        requests_by_session: dict[date, list[OrderRequest]] = defaultdict(list)
        for request in order_requests:
            if request.signal_session not in ordered_sessions:
                raise BacktestError("order signal session is outside the run")
            requests_by_session[request.signal_session].append(request)
        splits_by_session: dict[date, list[SplitEvent]] = defaultdict(list)
        for event in splits:
            if event.effective_session not in ordered_sessions:
                raise BacktestError("split effective session is outside the run")
            splits_by_session[event.effective_session].append(event)
        dividends_by_session: dict[date, list[DividendEvent]] = defaultdict(list)
        for event in dividends:
            if event.payment_session not in ordered_sessions:
                raise BacktestError("dividend payment session is outside the run")
            dividends_by_session[event.payment_session].append(event)
        terminations_by_session: dict[date, list[TerminationEvent]] = defaultdict(list)
        for event in terminations:
            if event.session not in ordered_sessions:
                raise BacktestError("termination session is outside the run")
            terminations_by_session[event.session].append(event)

        settled_cash = self.initial_settled_cash
        positions = {item.security_id: item for item in self.initial_positions}
        settlements: list[CashSettlement] = []
        orders: list[OrderRecord] = []
        fills: list[FillRecord] = []
        action_records: list[CorporateActionRecord] = []
        termination_records: list[TerminationRecord] = []
        nav_records: list[NavRecord] = []
        pending: list[_PendingOrder] = []
        retry_at_close: list[_PendingOrder] = []
        last_marks: dict[str, Decimal] = {}
        otc_events: dict[str, TerminationEvent] = {}
        cumulative_costs = Decimal(0)

        for session in ordered_sessions:
            due = [item for item in settlements if item.settlement_session == session]
            settled_cash = money(
                settled_cash + sum((item.amount for item in due), start=Decimal(0))
            )
            settlements = [item for item in settlements if item.settlement_session != session]

            for event in sorted(
                splits_by_session[session],
                key=lambda item: (item.security_id, item.evidence_pointer),
            ):
                position = positions.get(event.security_id)
                if position is None:
                    continue
                positions[event.security_id] = replace(
                    position,
                    quantity=position.quantity * event.new_for_old,
                    cost_basis_per_share=position.cost_basis_per_share / event.new_for_old,
                    high_water_mark=position.high_water_mark / event.new_for_old,
                )
                if event.security_id in last_marks:
                    last_marks[event.security_id] /= event.new_for_old
                action_records.append(
                    CorporateActionRecord(
                        event.security_id,
                        session,
                        "split",
                        Decimal(0),
                        event.evidence_pointer,
                    )
                )

            todays_pending = sorted(
                (item for item in pending if item.scheduled_session == session),
                key=lambda item: (item.side, item.root_order_id, item.attempt),
            )
            pending = [item for item in pending if item.scheduled_session != session]
            for order in todays_pending:
                bar = bar_index.get((session, order.security_id))
                open_price = None if bar is None else bar.open_price
                collar_passed = open_price is not None and (
                    open_price <= order.limit_price
                    if order.side is OrderSide.BUY
                    else open_price >= order.limit_price
                )
                if not collar_passed:
                    if order.side is OrderSide.BUY:
                        orders.append(
                            self._order_record(
                                order,
                                OrderStatus.CANCELLED,
                                "opening price absent or outside entry collar",
                            )
                        )
                    elif order.attempt >= 4:
                        orders.append(
                            self._order_record(
                                order,
                                OrderStatus.CANCELLED,
                                "four eligible opens exhausted",
                            )
                        )
                        raise ManualExecutionRequired(
                            f"{order.security_id} exit exhausted initial open plus three retries"
                        )
                    else:
                        orders.append(
                            self._order_record(
                                order,
                                OrderStatus.RETRY,
                                "opening price absent or outside exit collar",
                            )
                        )
                        retry_at_close.append(order)
                    continue

                assert open_price is not None
                notional = money(order.quantity * open_price)
                cost = self.cost_model.charge(notional)
                position = positions.get(order.security_id)
                if order.side is OrderSide.BUY:
                    required = money(notional + cost)
                    if required > settled_cash:
                        orders.append(
                            self._order_record(
                                order,
                                OrderStatus.CANCELLED,
                                "insufficient settled cash before auction",
                            )
                        )
                        continue
                    settled_cash = money(settled_cash - required)
                    if position is None:
                        positions[order.security_id] = LedgerPosition(
                            position_id=f"POS:{order.root_order_id}",
                            security_id=order.security_id,
                            ticker=order.ticker,
                            quantity=order.quantity,
                            cost_basis_per_share=required / order.quantity,
                            high_water_mark=open_price,
                            entry_session=session,
                        )
                    else:
                        new_quantity = position.quantity + order.quantity
                        positions[order.security_id] = replace(
                            position,
                            quantity=new_quantity,
                            cost_basis_per_share=(
                                position.cost_basis_per_share * position.quantity + required
                            )
                            / new_quantity,
                            high_water_mark=max(position.high_water_mark, open_price),
                        )
                    cash_delta = -required
                else:
                    if position is None or order.quantity > position.quantity:
                        raise BacktestError("sell quantity exceeds the held position")
                    proceeds = money(notional - cost)
                    settlement_session = self.calendar.next_session(session).label
                    settlements.append(
                        CashSettlement(
                            order.security_id,
                            session,
                            settlement_session,
                            proceeds,
                            bar.evidence_pointer if bar else "",
                        )
                    )
                    remaining = position.quantity - order.quantity
                    if remaining == 0:
                        del positions[order.security_id]
                    else:
                        positions[order.security_id] = replace(position, quantity=remaining)
                    cash_delta = proceeds

                cumulative_costs = money(cumulative_costs + cost)
                orders.append(self._order_record(order, OrderStatus.FILLED, "collar satisfied"))
                fill = FillRecord(
                    fill_id=f"FILL:{order.order_id}",
                    order_id=order.order_id,
                    security_id=order.security_id,
                    side=order.side,
                    session=session,
                    quantity=order.quantity,
                    assumed_price=open_price,
                    notional=notional,
                    cost=cost,
                    cash_delta=cash_delta,
                    fill_quality=bar.fill_quality if bar else "",
                    evidence_pointer=bar.evidence_pointer if bar else "",
                )
                fills.append(fill)
                otc_event = otc_events.pop(order.root_order_id, None)
                if otc_event is not None:
                    termination_records.append(
                        TerminationRecord(
                            order.security_id,
                            session,
                            TerminationKind.OTC_REMOVAL,
                            order.quantity,
                            cash_delta,
                            None,
                            None,
                            otc_event.evidence_pointer,
                        )
                    )

            for event in sorted(
                dividends_by_session[session],
                key=lambda item: (item.security_id, item.evidence_pointer),
            ):
                position = positions.get(event.security_id)
                if position is None:
                    continue
                cash_delta = money(position.quantity * event.cash_per_share)
                settled_cash = money(settled_cash + cash_delta)
                action_records.append(
                    CorporateActionRecord(
                        event.security_id,
                        session,
                        "cash_dividend",
                        cash_delta,
                        event.evidence_pointer,
                    )
                )

            for event in sorted(
                (
                    item
                    for item in terminations_by_session[session]
                    if item.kind is not TerminationKind.OTC_REMOVAL
                ),
                key=lambda item: (item.security_id, item.kind),
            ):
                position = positions.get(event.security_id)
                if position is None:
                    continue
                if (
                    event.kind is TerminationKind.ACQUISITION
                    and event.stock_consideration_requires_valuation
                    and event.stock_consideration_value_per_share is None
                ):
                    raise TerminationEvidenceError(
                        f"{event.security_id} stock consideration cannot be valued"
                    )
                if event.kind is TerminationKind.ACQUISITION:
                    if (
                        event.cash_consideration_per_share is None
                        and event.stock_consideration_value_per_share is None
                    ):
                        raise TerminationEvidenceError(
                            f"{event.security_id} acquisition has no documented consideration"
                        )
                    payout_per_share = (event.cash_consideration_per_share or Decimal(0)) + (
                        event.stock_consideration_value_per_share or Decimal(0)
                    )
                elif event.kind is TerminationKind.BANKRUPTCY:
                    payout_per_share = event.documented_recovery_per_share or Decimal(0)
                else:
                    payout_per_share = Decimal(0)
                primary_value = money(position.quantity * payout_per_share)
                final_close = event.final_close or last_marks.get(event.security_id)
                final_value = (
                    None if final_close is None else money(position.quantity * final_close)
                )
                haircut_value = (
                    None if final_value is None else money(final_value * Decimal("0.70"))
                )
                settled_cash = money(settled_cash + primary_value)
                del positions[event.security_id]
                termination_records.append(
                    TerminationRecord(
                        event.security_id,
                        session,
                        event.kind,
                        position.quantity,
                        primary_value,
                        final_value,
                        haircut_value,
                        event.evidence_pointer,
                    )
                )

            for security_id in sorted(positions):
                bar = bar_index.get((session, security_id))
                if bar is not None and bar.official_close is not None:
                    last_marks[security_id] = bar.official_close

            for failed in tuple(retry_at_close):
                if failed.scheduled_session != session:
                    continue
                reference = last_marks.get(failed.security_id)
                if reference is None:
                    raise BacktestError("exit retry has no latest official close reference")
                pending.append(
                    _PendingOrder(
                        root_order_id=failed.root_order_id,
                        security_id=failed.security_id,
                        ticker=failed.ticker,
                        side=failed.side,
                        quantity=failed.quantity,
                        reason=failed.reason,
                        signal_session=session,
                        scheduled_session=self.calendar.next_session(session).label,
                        reference_close=reference,
                        collar_fraction=failed.collar_fraction,
                        attempt=failed.attempt + 1,
                    )
                )
                retry_at_close.remove(failed)

            for request in sorted(requests_by_session[session], key=lambda item: item.order_id):
                bar = bar_index.get((session, request.security_id))
                if bar is None or bar.official_close is None:
                    raise BacktestError("order signal lacks an official reference close")
                pending.append(
                    self._pending_from_request(request, reference_close=bar.official_close)
                )

            for event in sorted(
                (
                    item
                    for item in terminations_by_session[session]
                    if item.kind is TerminationKind.OTC_REMOVAL
                ),
                key=lambda item: item.security_id,
            ):
                position = positions.get(event.security_id)
                if position is None:
                    continue
                reference = last_marks.get(event.security_id)
                if reference is None:
                    raise TerminationEvidenceError("OTC exit has no official signal close")
                root_order_id = f"TERM:OTC:{event.security_id}:{session.isoformat()}"
                request = OrderRequest(
                    root_order_id,
                    event.security_id,
                    position.ticker,
                    OrderSide.SELL,
                    position.quantity,
                    session,
                    OrderReason.OTC_EXIT,
                )
                pending.append(self._pending_from_request(request, reference_close=reference))
                otc_events[root_order_id] = event

            market_value = Decimal(0)
            for security_id, position in positions.items():
                mark = last_marks.get(security_id)
                if mark is None:
                    raise BacktestError(f"held security {security_id} has no closing mark")
                market_value += money(position.quantity * mark)
            unsettled_cash = money(sum((item.amount for item in settlements), start=Decimal(0)))
            nav_value = money(settled_cash + unsettled_cash + market_value)
            nav_records.append(
                NavRecord(
                    session,
                    settled_cash,
                    unsettled_cash,
                    money(market_value),
                    nav_value,
                    cumulative_costs,
                )
            )

        orders.extend(
            self._order_record(order, OrderStatus.PENDING, "scheduled beyond run end")
            for order in sorted(
                pending,
                key=lambda item: (
                    item.scheduled_session,
                    item.root_order_id,
                    item.attempt,
                ),
            )
        )
        return BacktestResult(
            LedgerSnapshot(
                identity=self.identity,
                settled_cash=settled_cash,
                unsettled_settlements=tuple(settlements),
                positions=tuple(sorted(positions.values(), key=lambda item: item.security_id)),
                orders=tuple(orders),
                fills=tuple(fills),
                actions=tuple(action_records),
                terminations=tuple(termination_records),
                nav=tuple(nav_records),
            )
        )
