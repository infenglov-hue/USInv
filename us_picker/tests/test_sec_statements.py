"""SEC companyfacts -> IsYatirim-shaped statements (PIT + calendarization)."""

from datetime import date

from us_picker.cleaning.financial_periods import compose_ttm_items
from us_picker.data.sources.sec_statements import (
    build_statements,
    calendar_quarter_end,
    discrete_quarters,
    first_filed_facts,
)


def _row(start, end, val, filed, form="10-Q", accn="a"):
    row = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        row["start"] = start
    return row


def _facts(concepts: dict, dei: dict | None = None) -> dict:
    gaap = {}
    for concept, rows in concepts.items():
        unit = "shares" if "Shares" in concept else "USD"
        gaap[concept] = {"units": {unit: rows}}
    payload = {"facts": {"us-gaap": gaap}}
    if dei:
        payload["facts"]["dei"] = {k: {"units": {"shares": v}} for k, v in dei.items()}
    return payload


def _value(row, code):
    return next(item["value"] for item in row["items"] if item["item_code"] == code)


def test_first_filed_value_wins_over_restatement():
    facts = _facts(
        {
            "Revenues": [
                _row("2024-01-01", "2024-03-31", 100, "2024-05-01"),
                _row("2024-01-01", "2024-03-31", 90, "2025-05-01"),  # restated comparative
            ]
        }
    )["facts"]
    [fact] = first_filed_facts(facts, "Revenues")
    assert fact.value == 100
    assert fact.filed == date(2024, 5, 1)


def test_discrete_quarters_derived_from_ytd_and_annual():
    rows = [
        _row("2024-01-01", "2024-03-31", 10, "2024-05-01"),
        _row("2024-01-01", "2024-06-30", 25, "2024-08-01"),
        _row("2024-01-01", "2024-09-30", 45, "2024-11-01"),
        _row("2024-01-01", "2024-12-31", 70, "2025-02-15", form="10-K"),
    ]
    quarters = discrete_quarters(first_filed_facts(_facts({"Revenues": rows})["facts"], "Revenues"))
    assert {q.end: q.value for q in quarters.values()} == {
        date(2024, 3, 31): 10,
        date(2024, 6, 30): 15,
        date(2024, 9, 30): 20,
        date(2024, 12, 31): 25,
    }
    assert quarters[date(2024, 12, 31)].filed == date(2025, 2, 15)


def test_q4_derived_when_nine_month_ytd_missing():
    rows = [
        _row("2024-01-01", "2024-03-31", 10, "2024-05-01"),
        _row("2024-04-01", "2024-06-30", 15, "2024-08-01"),
        _row("2024-07-01", "2024-09-30", 20, "2024-11-01"),
        _row("2024-01-01", "2024-12-31", 70, "2025-02-15", form="10-K"),
    ]
    quarters = discrete_quarters(first_filed_facts(_facts({"Revenues": rows})["facts"], "Revenues"))
    assert quarters[date(2024, 12, 31)].value == 25


def test_calendar_snap_for_non_december_fiscal_years():
    assert calendar_quarter_end(date(2025, 6, 28)) == date(2025, 6, 30)  # Apple
    assert calendar_quarter_end(date(2025, 1, 31)) == date(2024, 12, 31)  # Walmart
    assert calendar_quarter_end(date(2025, 5, 31)) == date(2025, 6, 30)  # Nike


def test_non_calendar_fiscal_year_builds_calendar_ytd_and_correct_ttm():
    # Fiscal year Oct-Sep; quarters end Dec/Mar/Jun/Sep (Apple-like).
    rows = [
        _row("2023-10-01", "2023-12-30", 100, "2024-02-01"),
        _row("2023-10-01", "2024-03-30", 190, "2024-05-01"),
        _row("2023-10-01", "2024-06-29", 270, "2024-08-01"),
        _row("2023-10-01", "2024-09-28", 380, "2024-11-01", form="10-K"),
        _row("2024-09-29", "2024-12-28", 120, "2025-01-31"),
        _row("2024-09-29", "2025-03-29", 220, "2025-05-02"),
    ]
    income = build_statements(
        _facts({"Revenues": rows, "NetIncomeLoss": [dict(r, val=r["val"] / 10) for r in rows]})
    )["INCOME"]

    # Calendar 2024: Q1=90 (Jan-Mar), Q2=80, Q3=110, Q4=120.
    assert _value(income[date(2024, 3, 31)], "3C") == 90
    assert _value(income[date(2024, 6, 30)], "3C") == 170
    assert _value(income[date(2024, 12, 31)], "3C") == 400
    assert income[date(2024, 12, 31)]["period_type"] == "ANNUAL"
    assert _value(income[date(2025, 3, 31)], "3C") == 100

    ttm = compose_ttm_items(
        income[date(2025, 3, 31)]["items"],
        income[date(2024, 12, 31)]["items"],
        income[date(2024, 3, 31)]["items"],
    )
    # TTM to calendar Q1-2025 = 80 + 110 + 120 + 100.
    assert next(i["value"] for i in ttm if i["item_code"] == "3C") == 410


def test_publication_date_is_day_after_core_filing():
    rows = [_row("2024-01-01", "2024-03-31", 10, "2024-05-01")]
    income = build_statements(_facts({"Revenues": rows, "NetIncomeLoss": rows}))["INCOME"]
    assert income[date(2024, 3, 31)]["publication_date"] == date(2024, 5, 2)


def test_item_first_filed_after_core_is_not_leaked():
    # Gross profit for Q1-2024 first appears a year later as a comparative.
    core = [_row("2024-01-01", "2024-03-31", 10, "2024-05-01")]
    late = [_row("2024-01-01", "2024-03-31", 4, "2025-05-01")]
    income = build_statements(
        _facts({"Revenues": core, "NetIncomeLoss": core, "GrossProfit": late})
    )["INCOME"]
    row = income[date(2024, 3, 31)]
    assert row["publication_date"] == date(2024, 5, 2)
    assert _value(row, "3D") is None


def test_concepts_merge_at_quarter_level_by_earliest_filing():
    # Q1 tagged with concept B, later quarters with concept A; A's Q1 value
    # only appears a year later.  The calendar Q2 YTD must not wait for it.
    concept_a = [
        _row("2025-01-01", "2025-03-31", 10, "2026-05-01"),
        _row("2025-04-01", "2025-06-30", 12, "2025-08-01"),
    ]
    concept_b = [_row("2025-01-01", "2025-03-31", 10, "2025-05-01")]
    ni = [
        _row("2025-01-01", "2025-03-31", 1, "2025-05-01"),
        _row("2025-04-01", "2025-06-30", 1, "2025-08-01"),
    ]
    income = build_statements(
        _facts(
            {
                "Revenues": concept_a,
                "RevenueFromContractWithCustomerExcludingAssessedTax": concept_b,
                "NetIncomeLoss": ni,
            }
        )
    )["INCOME"]
    assert _value(income[date(2025, 6, 30)], "3C") == 22
    assert income[date(2025, 6, 30)]["publication_date"] == date(2025, 8, 2)


def test_balance_shares_prefer_dei_cover_count_summed_across_classes():
    assets = [_row(None, "2025-06-30", 1000, "2025-08-01", accn="q2")]
    equity = [_row(None, "2025-06-30", 400, "2025-08-01", accn="q2")]
    dei = {
        "EntityCommonStockSharesOutstanding": [
            _row(None, "2025-07-25", 60, "2025-08-01", accn="q2"),
            _row(None, "2025-07-25", 40, "2025-08-01", accn="q2"),
        ]
    }
    balance = build_statements(
        _facts({"Assets": assets, "StockholdersEquity": equity}, dei=dei)
    )["BALANCE"]
    row = balance[date(2025, 6, 30)]
    assert _value(row, "2OA") == 100
    assert _value(row, "2N") == 400


def test_capex_is_negative_and_fcf_derived():
    cfo = [_row("2025-01-01", "2025-03-31", 50, "2025-05-01")]
    capex = [_row("2025-01-01", "2025-03-31", 20, "2025-05-01")]
    cashflow = build_statements(
        _facts(
            {
                "NetCashProvidedByUsedInOperatingActivities": cfo,
                "PaymentsToAcquirePropertyPlantAndEquipment": capex,
            }
        )
    )["CASHFLOW"]
    row = cashflow[date(2025, 3, 31)]
    assert _value(row, "4CAI") == -20
    assert _value(row, "4CB") == 30
