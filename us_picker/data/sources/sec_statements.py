"""SEC companyfacts -> IsYatirim-shaped financial statements.

The scoring stack inherited from BIST Picker reads statements as JSON item
lists keyed by IsYatirim item codes (``3C`` net sales, ``1BL`` total assets,
...) with the IsYatirim period convention:

* one row per *calendar* quarter end (Mar 31 / Jun 30 / Sep 30 / Dec 31),
* INCOME / CASHFLOW rows are cumulative calendar year-to-date
  (Q1 = 3 months, Q2 = 6 months, Q3 = 9 months, ANNUAL = 12 months),
* BALANCE rows are point-in-time stocks at the period end.

This module converts an SEC ``companyfacts`` payload into exactly that shape so
every downstream stage (TTM composition, cleaning, scorers, backtest) runs
unchanged.  The conversion rules:

1. **As-first-filed.**  For every concept and reporting interval only the value
   from the earliest ``filed`` date is kept.  Later restatements and prior-period
   comparatives never overwrite what the market saw first.
2. **Discrete quarters.**  US filers report YTD durations (and often 3-month
   durations).  Discrete fiscal quarters are recovered by differencing
   same-start intervals (6M - 3M, 9M - 6M, 12M - 9M, ...).
3. **Calendarization.**  Fiscal quarters are snapped to the nearest calendar
   quarter end (within 45 days) so non-December fiscal years (Apple, Walmart,
   Nike) produce the calendar YTD rows the TTM logic expects.
4. **Publication date** is the latest ``filed`` date among the facts used for a
   row, plus one day, because EDGAR filings accepted after the close must not
   be visible to that session's T-1 signal.

Missing values stay ``None``; nothing is ever fabricated as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Optional, Sequence

# ---------------------------------------------------------------------------
# Concept chains: first concept with a value for a period wins.
# ---------------------------------------------------------------------------

REVENUE = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenuesNetOfInterestExpense",
)
COST_OF_REVENUE = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
    "CostOfServices",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
)
GROSS_PROFIT = ("GrossProfit",)
OPERATING_INCOME = ("OperatingIncomeLoss",)
PRETAX_INCOME = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
)
INTEREST_EXPENSE = (
    "InterestExpense",
    "InterestExpenseNonoperating",
    "InterestExpenseDebt",
)
NET_INCOME_TOTAL = ("ProfitLoss", "NetIncomeLoss")
NET_INCOME_PARENT = (
    "NetIncomeLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "ProfitLoss",
)
DEPRECIATION = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "Depreciation",
)
CFO = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
)
WORKING_CAPITAL_INCREASE = ("IncreaseDecreaseInOperatingCapital",)

TOTAL_ASSETS = ("Assets",)
CURRENT_ASSETS = ("AssetsCurrent",)
CASH = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "Cash",
)
PPE = (
    "PropertyPlantAndEquipmentNet",
    "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
)
TOTAL_LIABILITIES = ("Liabilities",)
CURRENT_LIABILITIES = ("LiabilitiesCurrent",)
NONCURRENT_LIABILITIES = ("LiabilitiesNoncurrent",)
TOTAL_EQUITY = (
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "StockholdersEquity",
)
PARENT_EQUITY = ("StockholdersEquity",)
SHARES_INSTANT = ("CommonStockSharesOutstanding",)
SHARES_WEIGHTED = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)
DEI_SHARES = "EntityCommonStockSharesOutstanding"

ACCEPTED_FORMS = frozenset(
    {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT", "10-KT/A", "10-QT/A"}
)

# Duration buckets in days (inclusive start/end, 52/53-week years allowed).
_QUARTER_DAYS = (80, 100)
_MAX_SNAP_DAYS = 45
_CALENDAR_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))
_PERIOD_TYPE_BY_MONTH = {3: "Q1", 6: "Q2", 9: "Q3", 12: "ANNUAL"}


@dataclass(frozen=True)
class Fact:
    """One first-filed XBRL value."""

    start: Optional[date]
    end: date
    value: float
    filed: date


@dataclass(frozen=True)
class Quarter:
    """A discrete fiscal quarter (3-month flow)."""

    start: date
    end: date
    value: float
    filed: date


# ---------------------------------------------------------------------------
# Low-level fact extraction
# ---------------------------------------------------------------------------


def _parse_date(raw: object) -> Optional[date]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _unit_rows(facts: Mapping, taxonomy: str, concept: str, unit: str) -> list[dict]:
    node = (facts.get(taxonomy) or {}).get(concept) or {}
    return list((node.get("units") or {}).get(unit) or [])


def first_filed_facts(
    facts: Mapping,
    concept: str,
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    forms: frozenset[str] = ACCEPTED_FORMS,
) -> list[Fact]:
    """Return one fact per (start, end), keeping the earliest filing."""

    best: dict[tuple[Optional[date], date], Fact] = {}
    for row in _unit_rows(facts, taxonomy, concept, unit):
        if forms and row.get("form") not in forms:
            continue
        end = _parse_date(row.get("end"))
        filed = _parse_date(row.get("filed"))
        value = row.get("val")
        if end is None or filed is None or not isinstance(value, (int, float)):
            continue
        if isinstance(value, bool):
            continue
        start = _parse_date(row.get("start"))
        key = (start, end)
        current = best.get(key)
        if current is None or filed < current.filed:
            best[key] = Fact(start=start, end=end, value=float(value), filed=filed)
    return sorted(best.values(), key=lambda f: (f.end, f.start or f.end))


def _days(start: date, end: date) -> int:
    return (end - start).days + 1


def _is_quarter(start: date, end: date) -> bool:
    lo, hi = _QUARTER_DAYS
    return lo <= _days(start, end) <= hi


# ---------------------------------------------------------------------------
# Flow items: YTD/3M durations -> discrete quarters
# ---------------------------------------------------------------------------


def discrete_quarters(duration_facts: Iterable[Fact]) -> dict[date, Quarter]:
    """Recover discrete fiscal quarters keyed by quarter end date.

    Direct 3-month facts are used as-is.  Missing quarters are derived by
    differencing two intervals that share a start date and whose ends are one
    quarter apart (e.g. 9M YTD - 6M YTD).  Known consecutive quarters are also
    chained into synthetic YTD intervals so a 12M annual value can yield Q4
    even when the 9M YTD value was never tagged.
    """

    intervals: dict[tuple[date, date], tuple[float, date]] = {}
    for fact in duration_facts:
        if fact.start is None or fact.end < fact.start:
            continue
        key = (fact.start, fact.end)
        if key not in intervals or fact.filed < intervals[key][1]:
            intervals[key] = (fact.value, fact.filed)

    quarters: dict[date, Quarter] = {}
    for (start, end), (value, filed) in intervals.items():
        if _is_quarter(start, end):
            existing = quarters.get(end)
            if existing is None or filed < existing.filed:
                quarters[end] = Quarter(start, end, value, filed)

    for _ in range(6):  # a fiscal year has at most 4 quarters; closure converges fast
        added = False

        # Chain consecutive quarters into synthetic YTD intervals.
        by_start: dict[date, list[Quarter]] = {}
        for q in quarters.values():
            by_start.setdefault(q.start, []).append(q)
        for q in list(quarters.values()):
            total, filed, cursor_end = q.value, q.filed, q.end
            while True:
                nxt = next(
                    (
                        c
                        for c in by_start.get(cursor_end + timedelta(days=1), [])
                        if _is_quarter(c.start, c.end)
                    ),
                    None,
                )
                if nxt is None:
                    break
                total += nxt.value
                filed = max(filed, nxt.filed)
                cursor_end = nxt.end
                key = (q.start, cursor_end)
                if key not in intervals:
                    intervals[key] = (total, filed)
                    added = True

        # Difference same-start intervals whose ends are one quarter apart.
        grouped: dict[date, list[tuple[date, float, date]]] = {}
        for (start, end), (value, filed) in intervals.items():
            grouped.setdefault(start, []).append((end, value, filed))
        for start, rows in grouped.items():
            rows.sort()
            for i, (short_end, short_val, short_filed) in enumerate(rows):
                for long_end, long_val, long_filed in rows[i + 1 :]:
                    q_start = short_end + timedelta(days=1)
                    if not _is_quarter(q_start, long_end):
                        continue
                    if long_end in quarters:
                        continue
                    quarters[long_end] = Quarter(
                        start=q_start,
                        end=long_end,
                        value=long_val - short_val,
                        filed=max(short_filed, long_filed),
                    )
                    added = True
        if not added:
            break

    return quarters


def calendar_quarter_end(day: date) -> Optional[date]:
    """Snap a fiscal period end to the nearest calendar quarter end."""

    candidates = []
    for year in (day.year - 1, day.year, day.year + 1):
        for month, dom in _CALENDAR_QUARTER_ENDS:
            candidates.append(date(year, month, dom))
    best = min(candidates, key=lambda c: abs((c - day).days))
    if abs((best - day).days) > _MAX_SNAP_DAYS:
        return None
    return best


def calendarize_quarters(quarters: Mapping[date, Quarter]) -> dict[date, Quarter]:
    """Map fiscal quarters onto calendar quarter ends (closest wins)."""

    result: dict[date, Quarter] = {}
    for end, quarter in quarters.items():
        cal_end = calendar_quarter_end(end)
        if cal_end is None:
            continue
        current = result.get(cal_end)
        if current is None or abs((end - cal_end).days) < abs((current.end - cal_end).days):
            result[cal_end] = quarter
    return result


def calendar_ytd(cal_quarters: Mapping[date, Quarter]) -> dict[date, tuple[float, date]]:
    """Build calendar YTD values: {period_end: (value, latest_filed)}."""

    result: dict[date, tuple[float, date]] = {}
    years = {d.year for d in cal_quarters}
    for year in years:
        total = 0.0
        filed: Optional[date] = None
        for month, dom in _CALENDAR_QUARTER_ENDS:
            q = cal_quarters.get(date(year, month, dom))
            if q is None:
                break  # YTD needs every earlier quarter of the calendar year
            total += q.value
            filed = q.filed if filed is None else max(filed, q.filed)
            result[date(year, month, dom)] = (total, filed)
    return result


def _merge_earliest(
    merged: dict[date, tuple[float, date]],
    series: Mapping[date, tuple[float, date]],
) -> None:
    """Merge a concept's series, keeping whichever value was filed first.

    Companies switch tags over time and later filings re-tag old periods as
    comparatives.  The earliest-filed value is what the market could see;
    ties keep the earlier concept in the chain.
    """

    for period_end, payload in series.items():
        current = merged.get(period_end)
        if current is None or payload[1] < current[1]:
            merged[period_end] = payload


def _flow_series(facts: Mapping, chain: Sequence[str], *, unit: str = "USD") -> dict[date, tuple[float, date]]:
    """Calendar-YTD series across ``chain``.

    Concepts are merged at the discrete-quarter level (earliest-filed quarter
    wins) before YTD assembly: Alphabet tagged Q1-2025 revenue with one
    concept and Q2 with another, and the first concept's Q1 value only
    appeared a year later as a comparative.
    """

    merged: dict[date, Quarter] = {}
    for concept in chain:
        quarters = calendarize_quarters(
            discrete_quarters(first_filed_facts(facts, concept, unit=unit))
        )
        for cal_end, quarter in quarters.items():
            current = merged.get(cal_end)
            if current is None or quarter.filed < current.filed:
                merged[cal_end] = quarter
    return calendar_ytd(merged)


# ---------------------------------------------------------------------------
# Balance items: instants -> calendar quarter ends
# ---------------------------------------------------------------------------


def _instant_series(
    facts: Mapping,
    chain: Sequence[str],
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
) -> dict[date, tuple[float, date]]:
    merged: dict[date, tuple[float, date]] = {}
    for concept in chain:
        snapped: dict[date, tuple[int, float, date]] = {}
        for fact in first_filed_facts(facts, concept, taxonomy=taxonomy, unit=unit):
            if fact.start is not None:
                continue
            cal_end = calendar_quarter_end(fact.end)
            if cal_end is None:
                continue
            distance = abs((fact.end - cal_end).days)
            current = snapped.get(cal_end)
            if current is None or distance < current[0]:
                snapped[cal_end] = (distance, fact.value, fact.filed)
        _merge_earliest(merged, {k: (v, f) for k, (_, v, f) in snapped.items()})
    return merged


def _share_series(
    facts: Mapping,
) -> tuple[dict[date, tuple[float, date]], dict[date, date]]:
    """Shares outstanding per calendar quarter end, plus each count's as-of date.

    Priority: dei cover-page count (summed across share classes reported in
    the same filing), balance-sheet ``CommonStockSharesOutstanding``, then the
    quarter's weighted-average diluted/basic count.  The as-of (basis) date is
    the filing date; the fetcher uses it to normalize into split-free units.
    """

    # Filing accession -> calendar period end, learned from the Assets facts.
    accn_period: dict[str, date] = {}
    for row in _unit_rows(facts, "us-gaap", "Assets", "USD"):
        if row.get("form") not in ACCEPTED_FORMS:
            continue
        end = _parse_date(row.get("end"))
        accn = row.get("accn")
        if end is None or not accn:
            continue
        cal_end = calendar_quarter_end(end)
        if cal_end is None:
            continue
        previous = accn_period.get(accn)
        if previous is None or cal_end > previous:
            accn_period[accn] = cal_end  # a filing's own period is its latest balance date

    dei: dict[date, tuple[float, date]] = {}
    per_accn: dict[tuple[str, date], tuple[float, date]] = {}
    for row in _unit_rows(facts, "dei", DEI_SHARES, "shares"):
        if row.get("form") not in ACCEPTED_FORMS:
            continue
        accn = row.get("accn")
        filed = _parse_date(row.get("filed"))
        cover_end = _parse_date(row.get("end"))
        value = row.get("val")
        if not accn or filed is None or cover_end is None or not isinstance(value, (int, float)):
            continue
        period = accn_period.get(accn)
        if period is None:
            continue
        key = (accn, cover_end)
        total, _ = per_accn.get(key, (0.0, filed))
        per_accn[key] = (total + float(value), filed)
    for (accn, cover_end), (value, filed) in per_accn.items():
        period = accn_period[accn]
        current = dei.get(period)
        if current is None or filed < current[1]:
            dei[period] = (value, filed)

    merged = dict(dei)
    for period, payload in _instant_series(facts, SHARES_INSTANT, unit="shares").items():
        merged.setdefault(period, payload)
    # Weighted averages: prefer the shortest window ending at the period end
    # (fiscal Q4 only has the 12-month figure in the 10-K).
    weighted: dict[date, tuple[int, float, date]] = {}
    for concept in SHARES_WEIGHTED:
        for fact in first_filed_facts(facts, concept, unit="shares"):
            if fact.start is None:
                continue
            cal_end = calendar_quarter_end(fact.end)
            if cal_end is None:
                continue
            span = _days(fact.start, fact.end)
            current = weighted.get(cal_end)
            if current is None or span < current[0]:
                weighted[cal_end] = (span, fact.value, fact.filed)
    for cal_end, (_, value, filed) in weighted.items():
        merged.setdefault(cal_end, (value, filed))
    series = {k: v for k, v in merged.items() if v[0] > 0}
    # Scale guard: some filers tag counts "in thousands" by mistake. Drop any
    # value below 1% of the company's median count.
    if series:
        values = sorted(v[0] for v in series.values())
        median = values[len(values) // 2]
        series = {k: v for k, v in series.items() if v[0] >= 0.01 * median}
    # Share counts are stated in the split basis current when the filing was
    # issued (splits before issuance are applied retroactively, including to
    # comparatives), so the filing date is each count's basis date.
    as_of = {k: v[1] for k, v in series.items()}
    return series, as_of


# ---------------------------------------------------------------------------
# Statement assembly
# ---------------------------------------------------------------------------


def _combine(
    *series: Mapping[date, tuple[float, date]],
    op,
) -> dict[date, tuple[float, date]]:
    """Element-wise combination defined only where every input has a value."""

    if not series:
        return {}
    keys = set(series[0])
    for s in series[1:]:
        keys &= set(s)
    result = {}
    for key in keys:
        values = [s[key][0] for s in series]
        filed = max(s[key][1] for s in series)
        result[key] = (op(*values), filed)
    return result


def _with_fallback(
    primary: Mapping[date, tuple[float, date]],
    fallback: Mapping[date, tuple[float, date]],
) -> dict[date, tuple[float, date]]:
    result = dict(fallback)
    result.update(primary)
    return result


def _negate(series: Mapping[date, tuple[float, date]]) -> dict[date, tuple[float, date]]:
    return {k: (-v, f) for k, (v, f) in series.items()}


@dataclass(frozen=True)
class ItemSpec:
    code: str
    label: str
    series: Mapping[date, tuple[float, date]]
    core: bool = False


def _statement_rows(specs: Sequence[ItemSpec]) -> dict[date, dict]:
    """Assemble one statement row per calendar period.

    The row becomes visible when its *core* items were filed.  Any other item
    first filed after that instant (typically a comparative re-tagged a year
    later) is left ``None`` rather than delaying the whole row or leaking a
    value the market could not see yet.
    """

    periods: set[date] = set()
    for spec in specs:
        periods |= set(spec.series)
    rows: dict[date, dict] = {}
    for period_end in sorted(periods):
        core_filed = [
            spec.series[period_end][1]
            for spec in specs
            if spec.core and period_end in spec.series
        ]
        if not core_filed:
            continue
        visible_on = max(core_filed)
        items = []
        for spec in specs:
            payload = spec.series.get(period_end)
            value = payload[0] if payload and payload[1] <= visible_on else None
            items.append(
                {
                    "item_code": spec.code,
                    "desc_tr": spec.label,
                    "desc_eng": spec.label,
                    "value": value,
                }
            )
        rows[period_end] = {
            "period_type": _PERIOD_TYPE_BY_MONTH[period_end.month],
            "publication_date": visible_on + timedelta(days=1),
            "items": items,
        }
    return rows


def build_statements(companyfacts: Mapping) -> dict[str, dict[date, dict]]:
    """Convert a companyfacts payload into IsYatirim-shaped statements.

    Returns ``{"INCOME": {...}, "BALANCE": {...}, "CASHFLOW": {...}}`` where
    each inner dict maps a calendar period end to
    ``{"period_type", "publication_date", "items"}``.
    """

    facts = companyfacts.get("facts") or {}

    revenue = _flow_series(facts, REVENUE)
    cost = _flow_series(facts, COST_OF_REVENUE)
    gross = _with_fallback(
        _flow_series(facts, GROSS_PROFIT),
        _combine(revenue, cost, op=lambda r, c: r - c),
    )
    pretax = _flow_series(facts, PRETAX_INCOME)
    interest = _flow_series(facts, INTEREST_EXPENSE)
    # EBIT fallback for filers without an operating-income subtotal (Nike):
    # pretax + interest expense, else pretax alone.
    operating = _with_fallback(
        _flow_series(facts, OPERATING_INCOME),
        _with_fallback(_combine(pretax, interest, op=lambda p, i: p + i), pretax),
    )
    net_total = _flow_series(facts, NET_INCOME_TOTAL)
    net_parent = _flow_series(facts, NET_INCOME_PARENT)
    depreciation = _flow_series(facts, DEPRECIATION)

    cfo = _flow_series(facts, CFO)
    capex = _negate(_flow_series(facts, CAPEX))  # IsYatirim convention: outflow < 0
    fcf = _combine(cfo, capex, op=lambda o, c: o + c)
    wc_change = _negate(_flow_series(facts, WORKING_CAPITAL_INCREASE))

    assets = _instant_series(facts, TOTAL_ASSETS)
    current_assets = _instant_series(facts, CURRENT_ASSETS)
    cash = _instant_series(facts, CASH)
    ppe = _instant_series(facts, PPE)
    liabilities = _instant_series(facts, TOTAL_LIABILITIES)
    current_liabilities = _instant_series(facts, CURRENT_LIABILITIES)
    total_equity = _instant_series(facts, TOTAL_EQUITY)
    parent_equity = _with_fallback(_instant_series(facts, PARENT_EQUITY), total_equity)
    noncurrent = _with_fallback(
        _instant_series(facts, NONCURRENT_LIABILITIES),
        _with_fallback(
            _combine(liabilities, current_liabilities, op=lambda t, c: t - c),
            _combine(assets, total_equity, current_liabilities, op=lambda a, e, c: a - e - c),
        ),
    )
    shares, shares_as_of = _share_series(facts)

    income = _statement_rows(
        [
            ItemSpec("3C", "Net Sales", revenue, core=True),
            ItemSpec("3D", "Gross Profit", gross),
            ItemSpec("3DF", "Operating Profit", operating),
            ItemSpec("3I", "Profit Before Tax", pretax),
            ItemSpec("3L", "Net Profit After Taxes", net_total, core=True),
            ItemSpec("3Z", "Net Profit Attributable to Parent", net_parent),
            ItemSpec("4B", "Depreciation & Amortization", depreciation),
        ]
    )
    cashflow = _statement_rows(
        [
            ItemSpec("4C", "Net Cash from Operating Activities", cfo, core=True),
            ItemSpec("4CAB", "Depreciation & Amortization", depreciation),
            ItemSpec("4CAI", "Capital Expenditures", capex),
            ItemSpec("4CB", "Free Cash Flow", fcf),
            ItemSpec("4CAF", "Change in Working Capital", wc_change),
        ]
    )
    balance = _statement_rows(
        [
            ItemSpec("1BL", "Total Assets", assets, core=True),
            ItemSpec("1A", "Current Assets", current_assets),
            ItemSpec("1AA", "Cash and Cash Equivalents", cash),
            ItemSpec("1BC", "Property, Plant & Equipment", ppe),
            ItemSpec("2A", "Current Liabilities", current_liabilities),
            ItemSpec("2B", "Long Term Liabilities", noncurrent),
            ItemSpec("2N", "Total Equity", total_equity, core=True),
            ItemSpec("2O", "Parent Shareholders Equity", parent_equity),
            # IsYatirim "share capital" doubles as share count (TRY 1 par);
            # here it carries shares outstanding directly.
            ItemSpec("2OA", "Shares Outstanding", shares),
        ]
    )
    for period_end, row in balance.items():
        for item in row["items"]:
            if item["item_code"] == "2OA" and item["value"] is not None:
                item["as_of"] = shares_as_of.get(period_end, period_end).isoformat()
    return {"INCOME": income, "BALANCE": balance, "CASHFLOW": cashflow}
