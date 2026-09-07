from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from usinv.data.edgar.fsds import FILINGS_SCHEMA
from usinv.data.edgar.pit_store import LATEST_FACT_SCHEMA, PIT_FACT_SCHEMA
from usinv.data.edgar.quarterly import QuarterlyFact, derive_quarterly_facts
from usinv.data.edgar.tag_chains import (
    CHAIN_VERSION,
    CONCEPT_CHAINS,
    PresentationRow,
    RawFact,
    StandardizationError,
    StandardizedFact,
    build_coverage_report,
    coverage_input,
    eligible_ciks_from_filings,
    standardize_facts,
    standardize_pit_snapshot,
)
from usinv.data.edgar.ttm import build_ttm_facts

CIK = 320193
TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "fundamentals" / "fundamentals_golden.json"


def _raw(
    tag: str,
    value: str,
    *,
    ddate: date = date(2025, 3, 31),
    qtrs: int = 1,
    form: str = "10-Q",
    accepted: datetime = datetime(2025, 5, 1, 20, tzinfo=UTC),
    adsh: str = "0000320193-25-000001",
    version: str = "us-gaap/2025",
    uom: str = "USD",
    cik: int = CIK,
) -> RawFact:
    return RawFact(
        cik=cik,
        tag=tag,
        ddate=ddate,
        qtrs=qtrs,
        uom=uom,
        value=Decimal(value),
        accepted=accepted,
        adsh=adsh,
        version=version,
        form=form,
        filed=accepted.date(),
        filing_period=ddate,
    )


def _standard(
    value: str,
    *,
    concept: str = "revenue",
    ddate: date,
    qtrs: int,
    form: str,
    accepted_day: int,
    tag: str = TAG,
    cik: int = CIK,
) -> StandardizedFact:
    period_kind = "instant" if qtrs == 0 else "duration"
    accepted = datetime(2026, 1, accepted_day, 20, tzinfo=UTC)
    adsh = f"0000320193-26-{accepted_day:06d}"
    return StandardizedFact(
        cik=cik,
        concept=concept,
        tier="core",
        period_kind=period_kind,
        ddate=ddate,
        qtrs=qtrs,
        uom="USD",
        value=Decimal(value),
        available_from=accepted,
        form=form,
        filed=accepted.date(),
        filing_period=ddate,
        source_tags=(tag,),
        source_adshs=(adsh,),
        source_acceptances=(accepted,),
        derivation=f"tag:{tag}",
    )


def test_full_versioned_concept_catalog_is_present() -> None:
    concepts = {chain.concept for chain in CONCEPT_CHAINS}

    assert CHAIN_VERSION == "usinv-sec-concepts-v5"
    assert len(CONCEPT_CHAINS) == 19
    assert sum(chain.tier == "core" for chain in CONCEPT_CHAINS) == 14
    assert sum(chain.tier == "secondary" for chain in CONCEPT_CHAINS) == 5
    assert {
        "revenue",
        "net_income",
        "gross_profit",
        "operating_income",
        "total_assets",
        "stockholders_equity",
        "cfo",
        "capex",
        "cash_and_equivalents",
        "long_term_debt",
        "current_debt_and_borrowings",
        "current_assets",
        "current_liabilities",
        "shares_outstanding",
        "preferred_equity",
        "minority_interest",
        "depreciation_amortization",
        "interest_expense",
        "income_tax_expense",
    } == concepts


def test_tag_precedence_derived_values_and_20f_exclusion() -> None:
    facts = [
        _raw("Revenues", "90"),
        _raw(TAG, "100"),
        _raw("CostOfRevenue", "60"),
        _raw("LongTermDebtCurrent", "7", qtrs=0),
        _raw("ShortTermBorrowings", "3", qtrs=0),
        _raw("NetIncomeLoss", "999", form="20-F", cik=999999),
    ]

    standardized = standardize_facts(facts)
    by_concept = {item.concept: item for item in standardized}

    assert by_concept["revenue"].value == Decimal("100")
    assert by_concept["revenue"].source_tags == (TAG,)
    assert by_concept["gross_profit"].value == Decimal("40")
    assert set(by_concept["gross_profit"].source_tags) == {TAG, "CostOfRevenue"}
    assert by_concept["current_debt_and_borrowings"].value == Decimal("10")
    assert not any(item.cik == 999999 for item in standardized)


def test_water_utility_total_operating_revenue_is_a_registered_fallback() -> None:
    standardized = standardize_facts([_raw("RegulatedOperatingRevenueWater", "700")])

    revenue = next(item for item in standardized if item.concept == "revenue")
    assert revenue.value == Decimal("700")
    assert revenue.source_tags == ("RegulatedOperatingRevenueWater",)


def test_custom_revenue_requires_unambiguous_top_line_pre_evidence() -> None:
    adsh = "0000813762-25-000001"
    custom = _raw(
        "NetSales",
        "42",
        adsh=adsh,
        version=adsh,
    )
    presentation = [
        PresentationRow(adsh, 2, 2, "IS", "NetSales", adsh, "Net sales"),
        PresentationRow(adsh, 2, 9, "IS", "OtherRevenue", adsh, "Other revenue"),
    ]

    standardized = standardize_facts([custom], presentation=presentation)

    revenue = next(item for item in standardized if item.concept == "revenue")
    assert revenue.value == Decimal("42")
    assert revenue.derivation == "custom-pre:is-top-line-revenue"

    ambiguous_fact = _raw("GrossSales", "45", adsh=adsh, version=adsh)
    ambiguous_pre = [
        *presentation,
        PresentationRow(adsh, 2, 2, "IS", "GrossSales", adsh, "Gross sales"),
    ]
    assert not standardize_facts([custom, ambiguous_fact], presentation=ambiguous_pre)


def test_calendar_filer_direct_ytd_q4_and_availability() -> None:
    rows = [
        _standard("10", ddate=date(2025, 3, 31), qtrs=1, form="10-Q", accepted_day=1),
        _standard("20", ddate=date(2025, 6, 30), qtrs=1, form="10-Q", accepted_day=2),
        _standard("60", ddate=date(2025, 9, 30), qtrs=3, form="10-Q", accepted_day=3),
        _standard("100", ddate=date(2025, 12, 31), qtrs=4, form="10-K", accepted_day=9),
    ]

    result = derive_quarterly_facts(rows)

    assert not result.quarantine
    assert [item.value for item in result.facts] == list(map(Decimal, ["10", "20", "30", "40"]))
    assert [item.method for item in result.facts] == [
        "direct",
        "direct",
        "ytd_difference",
        "annual_difference",
    ]
    assert result.facts[-1].available_from == rows[-1].available_from


def test_offset_fiscal_year_is_derived_from_period_ends_not_fy_fp() -> None:
    rows = [
        _standard("11", ddate=date(2024, 12, 28), qtrs=1, form="10-Q", accepted_day=1),
        _standard("21", ddate=date(2025, 3, 29), qtrs=1, form="10-Q", accepted_day=2),
        _standard("31", ddate=date(2025, 6, 28), qtrs=1, form="10-Q", accepted_day=3),
        _standard("104", ddate=date(2025, 9, 27), qtrs=4, form="10-K", accepted_day=9),
    ]

    result = derive_quarterly_facts(rows)

    assert not result.quarantine
    assert [item.fiscal_quarter for item in result.facts] == [1, 2, 3, 4]
    assert result.facts[-1].value == Decimal("41")
    assert all(item.fiscal_year_anchor == date(2025, 9, 27) for item in result.facts)


def test_open_fiscal_year_uses_only_already_published_annual_anchor() -> None:
    prior_annual = _standard(
        "80",
        ddate=date(2024, 12, 31),
        qtrs=4,
        form="10-K",
        accepted_day=1,
    )
    q1 = _standard(
        "25",
        ddate=date(2025, 3, 31),
        qtrs=1,
        form="10-Q",
        accepted_day=2,
    )

    result = derive_quarterly_facts([prior_annual, q1])
    open_q1 = next(item for item in result.facts if item.quarter_end == q1.ddate)

    assert open_q1.fiscal_year_anchor == prior_annual.ddate
    assert open_q1.calendar_anchor_adsh == prior_annual.source_adshs[0]
    assert open_q1.available_from == q1.available_from
    assert open_q1.available_from < datetime(2026, 2, 1, tzinfo=UTC)


def test_invalid_q4_is_quarantined_but_signed_magnitude_is_allowed() -> None:
    revenue_rows = [
        _standard("40", ddate=date(2025, 3, 31), qtrs=1, form="10-Q", accepted_day=1),
        _standard("40", ddate=date(2025, 6, 30), qtrs=1, form="10-Q", accepted_day=2),
        _standard("40", ddate=date(2025, 9, 30), qtrs=1, form="10-Q", accepted_day=3),
        _standard("100", ddate=date(2025, 12, 31), qtrs=4, form="10-K", accepted_day=9),
    ]
    revenue = derive_quarterly_facts(revenue_rows)
    assert len(revenue.facts) == 3
    assert revenue.quarantine[0].reason == "nonnegative_concept_negative"

    net_income_rows = [
        _standard(
            "-100",
            concept="net_income",
            tag="NetIncomeLoss",
            ddate=date(2025, 3, 31),
            qtrs=1,
            form="10-Q",
            accepted_day=1,
        ),
        _standard(
            "0",
            concept="net_income",
            tag="NetIncomeLoss",
            ddate=date(2025, 6, 30),
            qtrs=1,
            form="10-Q",
            accepted_day=2,
        ),
        _standard(
            "0",
            concept="net_income",
            tag="NetIncomeLoss",
            ddate=date(2025, 9, 30),
            qtrs=1,
            form="10-Q",
            accepted_day=3,
        ),
        _standard(
            "10",
            concept="net_income",
            tag="NetIncomeLoss",
            ddate=date(2025, 12, 31),
            qtrs=4,
            form="10-K",
            accepted_day=9,
        ),
    ]
    net_income = derive_quarterly_facts(net_income_rows)
    assert not net_income.quarantine
    assert net_income.facts[-1].value == Decimal("110")


def test_source_tag_mismatch_and_transition_year_fail_closed() -> None:
    mismatch = [
        _standard("10", ddate=date(2025, 3, 31), qtrs=1, form="10-Q", accepted_day=1),
        _standard("20", ddate=date(2025, 6, 30), qtrs=1, form="10-Q", accepted_day=2),
        _standard("30", ddate=date(2025, 9, 30), qtrs=1, form="10-Q", accepted_day=3),
        _standard(
            "100",
            ddate=date(2025, 12, 31),
            qtrs=4,
            form="10-K",
            accepted_day=9,
            tag="Revenues",
        ),
    ]
    result = derive_quarterly_facts(mismatch)
    assert len(result.facts) == 3
    assert result.quarantine[0].reason == "concept_or_source_tag_mismatch"

    transition = [_standard("100", ddate=date(2025, 12, 31), qtrs=4, form="10-KT", accepted_day=9)]
    result = derive_quarterly_facts(transition)
    assert not result.facts
    assert result.quarantine[0].reason == "fiscal_year_change_or_10_kt"


def _quarterly(
    fiscal_quarter: int,
    end: date,
    value: str,
    accepted_day: int,
) -> QuarterlyFact:
    accepted = datetime(2026, 1, accepted_day, 20, tzinfo=UTC)
    return QuarterlyFact(
        cik=CIK,
        concept="revenue",
        period_kind="duration",
        fiscal_year_anchor=date(2024, 12, 31),
        fiscal_quarter=fiscal_quarter,
        quarter_end=end,
        uom="USD",
        value=Decimal(value),
        available_from=accepted,
        method="direct",
        source_tags=(TAG,),
        source_adshs=(f"adsh-{accepted_day}",),
        source_acceptances=(accepted,),
        calendar_anchor_adsh="annual-anchor",
        calendar_anchor_available_from=datetime(2024, 11, 1, 20, tzinfo=UTC),
        chain_version=CHAIN_VERSION,
    )


def test_ttm_requires_four_consecutive_quarters_and_uses_latest_availability() -> None:
    quarters = [
        _quarterly(1, date(2025, 3, 31), "10", 1),
        _quarterly(2, date(2025, 6, 30), "20", 2),
        _quarterly(3, date(2025, 9, 30), "30", 3),
        _quarterly(4, date(2025, 12, 31), "40", 9),
    ]

    ttm = build_ttm_facts(quarters)

    assert len(ttm) == 1
    assert ttm[0].value == Decimal("100")
    assert ttm[0].available_from == quarters[-1].available_from
    assert all(value <= ttm[0].available_from for value in ttm[0].source_acceptances)

    gapped = [*quarters[:3], _quarterly(4, date(2026, 6, 30), "40", 9)]
    assert not build_ttm_facts(gapped)


def test_coverage_report_uses_full_issuer_concept_denominator() -> None:
    sample = _standard(
        "1",
        ddate=date(2025, 12, 31),
        qtrs=4,
        form="10-K",
        accepted_day=9,
    )
    report = build_coverage_report([sample], eligible_ciks=[CIK, 999999], sample_quarter="2025q4")

    assert report.eligible_issuers == 2
    assert next(item for item in report.concepts if item.concept == "revenue").rate == 0.5
    assert report.core_rate == 1 / 28
    assert report.secondary_rate == 0
    assert not report.passed
    payload = json.loads(report.to_json())
    assert payload["chain_version"] == "usinv-sec-concepts-v5"
    assert payload["measurement"] == "strict_observed_presence"
    assert payload["enforcement"] == "phase_2_3_final_universe_applicability"
    assert payload["missing_value_policy"] == "missing_is_not_zero"
    assert payload["passed"] is False


def _golden_standard(
    case: dict[str, object],
    source: dict[str, object],
    *,
    ddate: date,
    qtrs: int,
    form: str,
) -> StandardizedFact:
    accepted = datetime.fromisoformat(str(source["accepted"]).replace("Z", "+00:00"))
    return StandardizedFact(
        cik=int(case["cik"]),
        concept=str(case["concept"]),
        tier="core",
        period_kind="instant" if qtrs == 0 else "duration",
        ddate=ddate,
        qtrs=qtrs,
        uom="USD",
        value=Decimal(str(source["value"])),
        available_from=accepted,
        form=form,
        filed=accepted.date(),
        filing_period=ddate,
        source_tags=(str(case["tag"]),),
        source_adshs=(str(source["adsh"]),),
        source_acceptances=(accepted,),
        derivation=f"tag:{case['tag']}",
    )


def test_five_hand_verified_company_golden_cases() -> None:
    fixture = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert fixture["source_archive"]["fsds_sha256"] == (
        "2b36ac3850c022cf19edd882e31c3c453c7666677b9fdd2e1f7748fdb5768c6e"
    )
    evidence_urls = [
        url
        for key, case in fixture.items()
        if key not in {"fixture_version", "source_archive"}
        for url in case["evidence_urls"]
    ]
    assert evidence_urls and all(
        url.startswith("https://www.sec.gov/Archives/") for url in evidence_urls
    )

    for key in ("calendar_fy", "offset_fy"):
        case = fixture[key]
        annual_end = date.fromisoformat(case["fiscal_year_end"])
        rows = [
            _golden_standard(
                case,
                quarter,
                ddate=date.fromisoformat(quarter["end"]),
                qtrs=1,
                form="10-Q",
            )
            for quarter in case["quarters"]
        ]
        rows.append(_golden_standard(case, case["annual"], ddate=annual_end, qtrs=4, form="10-K"))
        result = derive_quarterly_facts(rows)
        assert not result.quarantine
        assert result.facts[-1].value == Decimal(case["expected_q4"])
        assert result.facts[-1].available_from == rows[-1].available_from

    restater = fixture["restater"]
    pit = restater["pit"]
    pit_raw = RawFact(
        cik=restater["cik"],
        tag=restater["tag"],
        ddate=date.fromisoformat(restater["end"]),
        qtrs=0,
        uom="USD",
        value=Decimal(pit["value"]),
        accepted=datetime.fromisoformat(pit["accepted"].replace("Z", "+00:00")),
        adsh=pit["adsh"],
        version="us-gaap/2025",
        form=pit["form"],
        filed=date(2025, 11, 14),
        filing_period=date.fromisoformat(restater["end"]),
    )
    standardized_pit = standardize_facts([pit_raw])
    assert next(
        item for item in standardized_pit if item.concept == "total_assets"
    ).value == Decimal(pit["value"])
    assert Decimal(pit["value"]) != Decimal(restater["latest"]["value"])
    assert pit["accepted"] < restater["latest"]["accepted"]

    custom = fixture["custom_revenue_small_cap"]
    custom_raw = _raw(
        custom["tag"],
        custom["value"],
        cik=custom["cik"],
        ddate=date.fromisoformat(custom["end"]),
        qtrs=custom["qtrs"],
        accepted=datetime.fromisoformat(custom["accepted"].replace("Z", "+00:00")),
        adsh=custom["adsh"],
        version=custom["adsh"],
    )
    pre = custom["pre"]
    custom_result = standardize_facts(
        [custom_raw],
        presentation=[
            PresentationRow(
                custom["adsh"],
                pre["report"],
                pre["line"],
                pre["stmt"],
                custom["tag"],
                custom["adsh"],
                pre["plabel"],
            )
        ],
    )
    custom_revenue = next(item for item in custom_result if item.concept == "revenue")
    assert custom_revenue.value == Decimal(custom["value"])
    assert custom_revenue.derivation == "custom-pre:is-top-line-revenue"

    quarantine = fixture["quarantine"]
    annual = _golden_standard(
        quarantine,
        quarantine,
        ddate=date.fromisoformat(quarantine["end"]),
        qtrs=quarantine["qtrs"],
        form=quarantine["form"],
    )
    quarantined = derive_quarterly_facts([annual])
    assert not quarantined.facts
    assert quarantined.quarantine[0].reason == quarantine["expected_reason"]


def test_parquet_adapter_requires_pit_marker_and_filters_eligible_filers(tmp_path: Path) -> None:
    accepted = datetime(2025, 5, 1, 20, tzinfo=UTC)
    pit_row = {
        "cik": CIK,
        "tag": TAG,
        "ddate": date(2025, 3, 31),
        "qtrs": 1,
        "uom": "USD",
        "value": Decimal("100.0000"),
        "accepted": accepted,
        "adsh": "0000320193-25-000001",
        "version": "us-gaap/2025",
        "form": "10-Q",
        "filed": accepted.date(),
        "filing_period": date(2025, 3, 31),
        "filing_fy": 2025,
        "filing_fp": "Q1",
        "footnote": None,
        "batch_id": "fixture",
        "source_quarter": "2025q2",
        "source_sha256": "a" * 64,
    }
    pit_path = tmp_path / "facts_pit.parquet"
    pq.write_table(pa.Table.from_pylist([pit_row], schema=PIT_FACT_SCHEMA), pit_path)
    pre_path = tmp_path / "pre.parquet"
    pq.write_table(
        pa.table(
            {
                name: pa.array([], type=pa.string())
                for name in (
                    "adsh",
                    "report",
                    "line",
                    "stmt",
                    "inpth",
                    "rfile",
                    "tag",
                    "version",
                    "plabel",
                    "negating",
                )
            }
        ),
        pre_path,
    )

    def filing(cik: int, sic: int) -> dict[str, object]:
        return {
            "batch_id": "fixture",
            "source_quarter": "2025q2",
            "source_sha256": "a" * 64,
            "adsh": f"{cik:010d}-25-000001",
            "cik": cik,
            "name": "Fixture",
            "sic": sic,
            "countryinc": "US",
            "former": None,
            "changed": None,
            "afs": "2",
            "fye": "1231",
            "form": "10-Q",
            "period": date(2025, 3, 31),
            "fy": 2025,
            "fp": "Q1",
            "filed": accepted.date(),
            "accepted": accepted,
            "accepted_raw": "20250501160000",
            "prevrpt": False,
            "instance": "fixture.xml",
            "nciks": 1,
            "aciks": None,
        }

    filings_path = tmp_path / "filings.parquet"
    pq.write_table(
        pa.Table.from_pylist([filing(CIK, 3571), filing(999999, 6022)], schema=FILINGS_SCHEMA),
        filings_path,
    )

    standardized = standardize_pit_snapshot(pit_path, presentation_paths=[pre_path])
    eligible = eligible_ciks_from_filings([filings_path])
    evidence = coverage_input(pit_path, role="facts_pit")

    assert len(standardized) == 1 and standardized[0].value == Decimal("100.0000")
    assert eligible == {CIK}
    assert evidence.row_count == 1 and len(evidence.content_sha256) == 64

    latest_path = tmp_path / "facts_latest.parquet"
    pq.write_table(pa.Table.from_pylist([pit_row], schema=LATEST_FACT_SCHEMA), latest_path)
    with pytest.raises(StandardizationError, match="verified facts_pit"):
        standardize_pit_snapshot(latest_path)
