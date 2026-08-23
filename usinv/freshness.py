"""Phase 2.4 freshness and delisted-coverage gates (DATA_SPEC §8, §6; OPS_SPEC §3).

The nightly-data job runs these as HARD gates: any red gate blocks delivery and
exits red (the "BIST lesson" kill-switch). Thresholds are config-tunable
(``usinv/config/freshness.yaml``); the blueprint defaults in DATA_SPEC §8 are the
initial values. The gate functions here are pure and operate on explicit inputs
so they can be exercised by fixtures; adapters that extract these inputs from the
real pipeline artifacts live with the callers.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from usinv.calendar import EXCHANGE_TIMEZONE, XNYSCalendar, default_calendar
from usinv.config.loader import FreshnessConfig

FILER_CATEGORIES = frozenset({"large_accelerated", "accelerated", "other"})
PERIODIC_FORMS = frozenset({"10-K", "10-Q"})


class FreshnessGateError(ValueError):
    """Raised when a freshness / coverage gate is red (nightly kill-switch)."""


# --------------------------------------------------------------------------- #
# Inputs (one record per checked unit; adapters build these from artifacts).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FundamentalsFreshnessInput:
    """The next periodic report a scored security still owes at the cutoff."""

    security_id: str
    filer_category: str
    next_form: str
    next_period_end: date

    def __post_init__(self) -> None:
        if self.filer_category not in FILER_CATEGORIES:
            raise FreshnessGateError(f"unknown filer category {self.filer_category!r}")
        if self.next_form not in PERIODIC_FORMS:
            raise FreshnessGateError(f"unknown periodic form {self.next_form!r}")


@dataclass(frozen=True, slots=True)
class PriceFreshnessInput:
    security_id: str
    last_bar_session: date


@dataclass(frozen=True, slots=True)
class DelistedAuditInput:
    active_symbols: frozenset[str]
    delisted_symbols: frozenset[str]
    master_active_symbols: frozenset[str]


@dataclass(frozen=True, slots=True)
class MacroFreshnessInput:
    series_id: str
    last_observation: date
    cadence_days: int


# --------------------------------------------------------------------------- #
# Per-gate results.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class FundamentalsFreshnessResult:
    scored_total: int
    stale_security_ids: tuple[str, ...]
    stale_fraction: float
    red: bool


@dataclass(frozen=True, slots=True)
class PriceFreshnessResult:
    checked_total: int
    stale_security_ids: tuple[str, ...]
    cutoff_session: date | None
    red: bool


@dataclass(frozen=True, slots=True)
class DelistedAuditResult:
    unexplained_symbols: tuple[str, ...]
    red: bool


@dataclass(frozen=True, slots=True)
class MacroFreshnessResult:
    stale_series_ids: tuple[str, ...]
    red: bool


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    as_of: datetime
    fundamentals: FundamentalsFreshnessResult
    prices: PriceFreshnessResult
    delisted: DelistedAuditResult
    macro: MacroFreshnessResult | None

    @property
    def red_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        fund = self.fundamentals
        if fund.red:
            reasons.append(
                f"fundamentals: {len(fund.stale_security_ids)}/{fund.scored_total} scored "
                f"securities stale ({fund.stale_fraction:.1%})"
            )
        if self.prices.red:
            reasons.append(
                f"prices: {len(self.prices.stale_security_ids)} active securities with a stale "
                "last bar"
            )
        if self.delisted.red:
            reasons.append(
                f"delisted-audit: {len(self.delisted.unexplained_symbols)} unexplained listing gaps"
            )
        if self.macro is not None and self.macro.red:
            reasons.append(f"macro: {len(self.macro.stale_series_ids)} series past max age")
        return tuple(reasons)

    @property
    def passed(self) -> bool:
        return not self.red_reasons

    def health_block(self) -> dict[str, object]:
        """Return the JSON-serialisable freshness/coverage health block (OPS_SPEC §3)."""
        block: dict[str, object] = {
            "as_of": self.as_of.isoformat(),
            "passed": self.passed,
            "red_reasons": list(self.red_reasons),
            "fundamentals": {
                "scored_total": self.fundamentals.scored_total,
                "stale": len(self.fundamentals.stale_security_ids),
                "stale_fraction": self.fundamentals.stale_fraction,
                "red": self.fundamentals.red,
            },
            "prices": {
                "checked_total": self.prices.checked_total,
                "stale": len(self.prices.stale_security_ids),
                "red": self.prices.red,
            },
            "delisted_audit": {
                "unexplained": len(self.delisted.unexplained_symbols),
                "red": self.delisted.red,
            },
        }
        if self.macro is not None:
            block["macro"] = {
                "stale": len(self.macro.stale_series_ids),
                "red": self.macro.red,
            }
        return block


# --------------------------------------------------------------------------- #
# Calendar helpers.
# --------------------------------------------------------------------------- #
def _add_business_days(day: date, count: int, calendar: XNYSCalendar) -> date:
    """Return the XNYS session *count* sessions after *day* (grace application)."""
    if count <= 0:
        return day
    label = day
    for _ in range(count):
        label = calendar.next_session(label).label
    return label


def _sessions_back(as_of_date: date, count: int, calendar: XNYSCalendar) -> date:
    """Return the session *count* sessions before the last session on/before *as_of_date*."""
    anchor = (
        as_of_date
        if calendar.is_session(as_of_date)
        else calendar.previous_session(as_of_date).label
    )
    label = anchor
    for _ in range(count):
        label = calendar.previous_session(label).label
    return label


def _add_months(day: date, months: int) -> date:
    """Add whole months to *day*, clamping to the target month's last day."""
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    last_day = monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def infer_next_periodic(latest_form: str, latest_period_end: date) -> tuple[str, date]:
    """Estimate the next periodic report's form and fiscal period end.

    Heuristic default: the next report is a 10-Q one fiscal quarter later. A
    fiscal-year-end quarter files a 10-K instead, but that requires the issuer's
    fiscal calendar; adapters that know it should override this. The 10-Q window
    is the stricter default and is config-tunable.
    """
    if latest_form not in PERIODIC_FORMS:
        raise FreshnessGateError(f"unknown periodic form {latest_form!r}")
    return "10-Q", _add_months(latest_period_end, 3)


# --------------------------------------------------------------------------- #
# Gate evaluators.
# --------------------------------------------------------------------------- #
def filing_deadline(
    entry: FundamentalsFreshnessInput,
    config: FreshnessConfig,
    calendar: XNYSCalendar,
) -> date:
    """Return the last date by which *entry*'s next report should be on file."""
    window = config.due_window_days(entry.next_form, entry.filer_category)
    base = entry.next_period_end + timedelta(days=window)
    return _add_business_days(base, config.fundamentals_grace_business_days, calendar)


def evaluate_fundamentals_freshness(
    inputs: Iterable[FundamentalsFreshnessInput],
    scored_total: int,
    *,
    as_of: date,
    config: FreshnessConfig,
    calendar: XNYSCalendar | None = None,
) -> FundamentalsFreshnessResult:
    """Flag scored securities whose next periodic report is past expected+grace."""
    if scored_total < 0:
        raise FreshnessGateError("scored_total cannot be negative")
    calendar = calendar or default_calendar()
    stale = tuple(
        entry.security_id for entry in inputs if as_of > filing_deadline(entry, config, calendar)
    )
    fraction = len(stale) / scored_total if scored_total else 0.0
    red = fraction > config.fundamentals_stale_fraction_max
    return FundamentalsFreshnessResult(scored_total, stale, fraction, red)


def evaluate_price_freshness(
    inputs: Iterable[PriceFreshnessInput],
    *,
    as_of: date,
    config: FreshnessConfig,
    calendar: XNYSCalendar | None = None,
) -> PriceFreshnessResult:
    """Flag active-universe securities whose last bar is older than the session floor."""
    calendar = calendar or default_calendar()
    entries = tuple(inputs)
    if not entries:
        return PriceFreshnessResult(0, (), None, False)
    cutoff = _sessions_back(as_of, config.price_max_sessions_stale, calendar)
    stale = tuple(entry.security_id for entry in entries if entry.last_bar_session < cutoff)
    return PriceFreshnessResult(len(entries), stale, cutoff, bool(stale))


def evaluate_delisted_audit(entry: DelistedAuditInput) -> DelistedAuditResult:
    """Flag master-active symbols absent from BOTH the active and delisted Alpha lists."""
    listed = entry.active_symbols | entry.delisted_symbols
    unexplained = tuple(sorted(entry.master_active_symbols - listed))
    return DelistedAuditResult(unexplained, bool(unexplained))


def evaluate_macro_freshness(
    inputs: Iterable[MacroFreshnessInput],
    *,
    as_of: date,
    config: FreshnessConfig,
) -> MacroFreshnessResult:
    """Flag macro series older than the configured multiple of their cadence."""
    stale = tuple(
        entry.series_id
        for entry in inputs
        if (as_of - entry.last_observation).days
        > config.macro_max_age_cadence_multiple * entry.cadence_days
    )
    return MacroFreshnessResult(stale, bool(stale))


# --------------------------------------------------------------------------- #
# Aggregate report + kill-switch.
# --------------------------------------------------------------------------- #
def build_freshness_report(
    *,
    as_of: datetime,
    fundamentals_inputs: Iterable[FundamentalsFreshnessInput],
    scored_total: int,
    price_inputs: Iterable[PriceFreshnessInput],
    delisted: DelistedAuditInput,
    config: FreshnessConfig,
    macro_inputs: Iterable[MacroFreshnessInput] | None = None,
    calendar: XNYSCalendar | None = None,
) -> FreshnessReport:
    """Assemble every freshness gate into one report evaluated at *as_of*."""
    if as_of.tzinfo is None:
        raise FreshnessGateError("freshness as_of must be timezone-aware")
    calendar = calendar or default_calendar()
    as_of_date = as_of.astimezone(EXCHANGE_TIMEZONE).date()
    fundamentals = evaluate_fundamentals_freshness(
        fundamentals_inputs, scored_total, as_of=as_of_date, config=config, calendar=calendar
    )
    prices = evaluate_price_freshness(
        price_inputs, as_of=as_of_date, config=config, calendar=calendar
    )
    delisted_result = evaluate_delisted_audit(delisted)
    macro_result = (
        evaluate_macro_freshness(macro_inputs, as_of=as_of_date, config=config)
        if macro_inputs is not None
        else None
    )
    return FreshnessReport(
        as_of=as_of,
        fundamentals=fundamentals,
        prices=prices,
        delisted=delisted_result,
        macro=macro_result,
    )


def enforce_freshness_gate(report: FreshnessReport) -> None:
    """Kill-switch: raise if any freshness/coverage gate is red so the job exits red."""
    if not report.passed:
        raise FreshnessGateError("; ".join(report.red_reasons))
