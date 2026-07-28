"""Rolling 21-session one-way turnover hard cap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class TurnoverError(ValueError):
    """Raised when turnover inputs cannot support an auditable decision."""


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class TurnoverEvent:
    session: date
    side: TradeSide
    notional: Decimal
    security_id: str
    forced: bool = False

    def __post_init__(self) -> None:
        if not self.security_id or not self.notional.is_finite() or self.notional <= 0:
            raise TurnoverError("turnover events require identity and positive finite notional")


@dataclass(frozen=True, slots=True)
class ProposedTrade:
    security_id: str
    side: TradeSide
    notional: Decimal
    forced: bool = False


@dataclass(frozen=True, slots=True)
class TurnoverDecision:
    allowed: tuple[ProposedTrade, ...]
    blocked: tuple[ProposedTrade, ...]
    rolling_fraction: Decimal
    forced_breach: bool


def enforce_turnover_budget(
    history: tuple[TurnoverEvent, ...],
    proposed: tuple[ProposedTrade, ...],
    *,
    eligible_window_sessions: frozenset[date],
    pre_trade_nav: Decimal,
    maximum_fraction: Decimal,
    initial_formation: bool = False,
) -> TurnoverDecision:
    """Admit trades in supplied deterministic order under the rolling cap.

    Forced exits always pass and may breach the cap. Once the cap is consumed,
    later discretionary sells or buys are blocked. Initial formation is
    separately reported and exempt.
    """
    if (
        not pre_trade_nav.is_finite()
        or pre_trade_nav <= 0
        or not maximum_fraction.is_finite()
        or not 0 <= maximum_fraction <= 1
    ):
        raise TurnoverError("turnover NAV/fraction inputs are invalid")
    relevant = tuple(item for item in history if item.session in eligible_window_sessions)
    buys = sum((item.notional for item in relevant if item.side is TradeSide.BUY), Decimal(0))
    sells = sum((item.notional for item in relevant if item.side is TradeSide.SELL), Decimal(0))
    allowed: list[ProposedTrade] = []
    blocked: list[ProposedTrade] = []
    forced_breach = False
    for trade in proposed:
        if not trade.security_id or not trade.notional.is_finite() or trade.notional <= 0:
            raise TurnoverError("proposed trades require identity and positive finite notional")
        next_buys = buys + (trade.notional if trade.side is TradeSide.BUY else 0)
        next_sells = sells + (trade.notional if trade.side is TradeSide.SELL else 0)
        fraction = Decimal("0.5") * (next_buys + next_sells) / pre_trade_nav
        if initial_formation or trade.forced or fraction <= maximum_fraction:
            allowed.append(trade)
            buys, sells = next_buys, next_sells
            if trade.forced and fraction > maximum_fraction:
                forced_breach = True
        else:
            blocked.append(trade)
    rolling = Decimal("0.5") * (buys + sells) / pre_trade_nav
    return TurnoverDecision(tuple(allowed), tuple(blocked), rolling, forced_breach)
