"""Versioned, provenance-preserving SEC concept fallback chains."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import duckdb
import pyarrow.parquet as pq

from usinv.data.edgar.client import EdgarError

CHAIN_VERSION: Final = "usinv-sec-concepts-v2"
STRICT_COVERAGE_MEASUREMENT: Final = "strict_observed_presence"
COVERAGE_ENFORCEMENT: Final = "phase_2_3_final_universe_applicability"
MISSING_VALUE_POLICY: Final = "missing_is_not_zero"
EXCLUDED_FORMS: Final = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})
Tier = Literal["core", "secondary"]
PeriodKind = Literal["duration", "instant"]
UnitKind = Literal["monetary", "shares"]


class StandardizationError(EdgarError):
    """Raised when raw facts violate the standardization contract."""


@dataclass(frozen=True, slots=True)
class ConceptChain:
    """One ordered, versioned mapping from SEC tags to a model concept."""

    concept: str
    tier: Tier
    period_kind: PeriodKind
    unit_kind: UnitKind
    tags: tuple[str, ...]
    nonnegative: bool = False


# The order is part of CHAIN_VERSION. These fallbacks deliberately omit the
# upstream standardizer's unsafe "missing means zero" and cross-concept copies.
CONCEPT_CHAINS: Final = (
    ConceptChain(
        "revenue",
        "core",
        "duration",
        "monetary",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RegulatedAndUnregulatedOperatingRevenue",
            "HealthCareOrganizationRevenue",
            "HealthCareOrganizationPatientServiceRevenue",
            "ContractsRevenue",
            "RealEstateRevenueNet",
            "OperatingLeasesIncomeStatementLeaseRevenue",
            "LicensesRevenue",
            "RoyaltyRevenue",
            "OilAndGasSalesRevenue",
            "TechnologyServicesRevenue",
            "SubscriptionRevenue",
            "AdvertisingRevenue",
            "PassengerRevenue",
            "RevenuesExcludingInterestAndDividends",
            "RevenuesNetOfInterestExpense",
            "RegulatedOperatingRevenue",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "net_income",
        "core",
        "duration",
        "monetary",
        (
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "IncomeLossAttributableToParent",
        ),
    ),
    ConceptChain("gross_profit", "core", "duration", "monetary", ("GrossProfit",)),
    ConceptChain(
        "operating_income",
        "core",
        "duration",
        "monetary",
        ("OperatingIncomeLoss",),
    ),
    ConceptChain("total_assets", "core", "instant", "monetary", ("Assets", "AssetsNet")),
    ConceptChain(
        "stockholders_equity",
        "core",
        "instant",
        "monetary",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "PartnersCapital",
        ),
    ),
    ConceptChain(
        "cfo",
        "core",
        "duration",
        "monetary",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    ConceptChain(
        "capex",
        "core",
        "duration",
        "monetary",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PaymentsForAdditionsToPropertyPlantAndEquipment",
            "PaymentsToAcquireOtherProductiveAssets",
            "PaymentsToAcquireOtherPropertyPlantAndEquipment",
            "PaymentsToAcquireMachineryAndEquipment",
            "PaymentsToAcquireOilAndGasPropertyAndEquipment",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "cash_and_equivalents",
        "core",
        "instant",
        "monetary",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
            "CashAndDueFromBanks",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "long_term_debt",
        "core",
        "instant",
        "monetary",
        (
            "LongTermDebtNoncurrent",
            "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
            "LongTermDebtAndCapitalLeaseObligations",
            "LongTermDebt",
            "FinanceLeaseLiabilityNoncurrent",
            "CapitalLeaseObligationsNoncurrent",
            "ConvertibleDebtNoncurrent",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "current_debt_and_borrowings",
        "core",
        "instant",
        "monetary",
        (
            "DebtCurrent",
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "LongTermDebtCurrent",
            "ShortTermBorrowings",
            "FinanceLeaseLiabilityCurrent",
            "CapitalLeaseObligationsCurrent",
            "ConvertibleDebtCurrent",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "current_assets",
        "core",
        "instant",
        "monetary",
        ("AssetsCurrent",),
        nonnegative=True,
    ),
    ConceptChain(
        "current_liabilities",
        "core",
        "instant",
        "monetary",
        ("LiabilitiesCurrent",),
        nonnegative=True,
    ),
    ConceptChain(
        "shares_outstanding",
        "core",
        "instant",
        "shares",
        ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
        nonnegative=True,
    ),
    ConceptChain(
        "preferred_equity",
        "secondary",
        "instant",
        "monetary",
        (
            "PreferredStockValue",
            "PreferredStockValueOutstanding",
            "PreferredStockNoParValue",
            "PreferredStocksIncludingAdditionalPaidInCapital",
            "RedeemablePreferredStockCarryingAmount",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "minority_interest",
        "secondary",
        "instant",
        "monetary",
        (
            "MinorityInterest",
            "NoncontrollingInterestInConsolidatedEntity",
            "RedeemableNoncontrollingInterestEquityCarryingAmount",
            "OtherMinorityInterests",
            "MinorityInterestInLimitedPartnerships",
            "MinorityInterestInOperatingPartnerships",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "depreciation_amortization",
        "secondary",
        "duration",
        "monetary",
        (
            "DepreciationDepletionAndAmortization",
            "DepreciationAndAmortization",
            "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
            "Depreciation",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "interest_expense",
        "secondary",
        "duration",
        "monetary",
        (
            "InterestExpense",
            "InterestExpenseDebt",
            "InterestExpenseNonoperating",
            "InterestExpenseOperating",
            "InterestExpenseOther",
            "InterestAndDebtExpense",
        ),
        nonnegative=True,
    ),
    ConceptChain(
        "income_tax_expense",
        "secondary",
        "duration",
        "monetary",
        ("IncomeTaxExpenseBenefit", "IncomeTaxExpenseBenefitContinuingOperations"),
    ),
)
CHAIN_BY_CONCEPT: Final = {chain.concept: chain for chain in CONCEPT_CHAINS}
_COST_OF_REVENUE_TAGS: Final = (
    "CostOfRevenue",
    "CostOfGoodsAndServicesSold",
    "CostOfGoodsSold",
)
_CUSTOM_LABEL = re.compile(r"\b(revenue|revenues|sales)\b", re.IGNORECASE)
_CUSTOM_REJECT = re.compile(
    r"\b(cost|segment|geograph|product|customer|related part|per share)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class RawFact:
    """The PIT fields needed by concept standardization."""

    cik: int
    tag: str
    ddate: date
    qtrs: int
    uom: str
    value: Decimal
    accepted: datetime
    adsh: str
    version: str
    form: str
    filed: date
    filing_period: date | None = None


@dataclass(frozen=True, slots=True)
class PresentationRow:
    """A PRE statement row used only for conservative custom-revenue recovery."""

    adsh: str
    report: int
    line: int
    stmt: str
    tag: str
    version: str
    plabel: str


@dataclass(frozen=True, slots=True)
class StandardizedFact:
    """One standardized value with complete source provenance."""

    cik: int
    concept: str
    tier: Tier
    period_kind: PeriodKind
    ddate: date
    qtrs: int
    uom: str
    value: Decimal
    available_from: datetime
    form: str
    filed: date
    filing_period: date | None
    source_tags: tuple[str, ...]
    source_adshs: tuple[str, ...]
    source_acceptances: tuple[datetime, ...]
    derivation: str
    chain_version: str = CHAIN_VERSION


@dataclass(frozen=True, slots=True)
class ConceptCoverage:
    concept: str
    tier: Tier
    issuers_with_value: int
    eligible_issuers: int

    @property
    def rate(self) -> float:
        return self.issuers_with_value / self.eligible_issuers if self.eligible_issuers else 0.0


@dataclass(frozen=True, slots=True)
class CoverageInput:
    role: str
    content_sha256: str
    byte_count: int
    row_count: int


@dataclass(frozen=True, slots=True)
class CoverageReport:
    sample_quarter: str
    chain_version: str
    eligible_issuers: int
    concepts: tuple[ConceptCoverage, ...]
    core_rate: float
    secondary_rate: float
    inputs: tuple[CoverageInput, ...] = ()
    core_threshold: float = 0.90
    secondary_threshold: float = 0.75

    @property
    def passed(self) -> bool:
        return (
            self.core_rate >= self.core_threshold
            and self.secondary_rate >= self.secondary_threshold
        )

    def to_json(self) -> str:
        payload = {
            "sample_quarter": self.sample_quarter,
            "chain_version": self.chain_version,
            "measurement": STRICT_COVERAGE_MEASUREMENT,
            "enforcement": COVERAGE_ENFORCEMENT,
            "missing_value_policy": MISSING_VALUE_POLICY,
            "eligible_issuers": self.eligible_issuers,
            "core_rate": self.core_rate,
            "secondary_rate": self.secondary_rate,
            "core_threshold": self.core_threshold,
            "secondary_threshold": self.secondary_threshold,
            "passed": self.passed,
            "inputs": [
                {
                    "role": item.role,
                    "content_sha256": item.content_sha256,
                    "byte_count": item.byte_count,
                    "row_count": item.row_count,
                }
                for item in self.inputs
            ],
            "concepts": [
                {
                    "concept": item.concept,
                    "tier": item.tier,
                    "issuers_with_value": item.issuers_with_value,
                    "eligible_issuers": item.eligible_issuers,
                    "rate": item.rate,
                }
                for item in self.concepts
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _valid_unit(chain: ConceptChain, uom: str) -> bool:
    if chain.unit_kind == "shares":
        return uom.lower() in {"shares", "share"}
    return bool(re.fullmatch(r"[A-Z]{3}", uom))


def _same_period_key(fact: RawFact) -> tuple[int, date, int, str]:
    return fact.cik, fact.ddate, fact.qtrs, fact.uom


def _from_sources(
    chain: ConceptChain,
    sources: Sequence[RawFact],
    *,
    value: Decimal,
    derivation: str,
) -> StandardizedFact:
    ordered = tuple(sorted(sources, key=lambda item: (item.accepted, item.adsh, item.tag)))
    if not ordered or any(item.accepted.tzinfo is None for item in ordered):
        raise StandardizationError("standardized sources require timezone-aware acceptance times")
    representative = max(ordered, key=lambda item: (item.accepted, item.adsh, item.tag))
    return StandardizedFact(
        cik=representative.cik,
        concept=chain.concept,
        tier=chain.tier,
        period_kind=chain.period_kind,
        ddate=representative.ddate,
        qtrs=representative.qtrs,
        uom=representative.uom,
        value=value,
        available_from=max(item.accepted for item in ordered),
        form=representative.form,
        filed=representative.filed,
        filing_period=representative.filing_period,
        source_tags=tuple(item.tag for item in ordered),
        source_adshs=tuple(item.adsh for item in ordered),
        source_acceptances=tuple(item.accepted for item in ordered),
        derivation=derivation,
    )


def _select_chain_fact(chain: ConceptChain, facts: Sequence[RawFact]) -> StandardizedFact | None:
    by_tag = {fact.tag: fact for fact in facts if _valid_unit(chain, fact.uom)}
    if chain.concept == "current_debt_and_borrowings":
        if direct := by_tag.get("DebtCurrent"):
            return _from_sources(chain, [direct], value=direct.value, derivation="tag:DebtCurrent")
        combined = by_tag.get("LongTermDebtAndCapitalLeaseObligationsCurrent")
        components = [combined] if combined is not None else []
        if combined is None and (long_term := by_tag.get("LongTermDebtCurrent")) is not None:
            components.append(long_term)
        if short_term := by_tag.get("ShortTermBorrowings"):
            components.append(short_term)
        if not components:
            lease_or_convertible = next(
                (
                    by_tag[tag]
                    for tag in (
                        "FinanceLeaseLiabilityCurrent",
                        "CapitalLeaseObligationsCurrent",
                        "ConvertibleDebtCurrent",
                    )
                    if tag in by_tag
                ),
                None,
            )
            if lease_or_convertible is not None:
                components.append(lease_or_convertible)
        if components:
            return _from_sources(
                chain,
                components,
                value=sum((item.value for item in components), Decimal(0)),
                derivation="sum:current-debt-components",
            )
        return None
    for tag in chain.tags:
        if source := by_tag.get(tag):
            return _from_sources(chain, [source], value=source.value, derivation=f"tag:{tag}")
    return None


def _derive_gross_profit(facts: Sequence[RawFact]) -> StandardizedFact | None:
    chain = CHAIN_BY_CONCEPT["gross_profit"]
    revenue = _select_chain_fact(CHAIN_BY_CONCEPT["revenue"], facts)
    by_tag = {fact.tag: fact for fact in facts if _valid_unit(chain, fact.uom)}
    cost = next((by_tag[tag] for tag in _COST_OF_REVENUE_TAGS if tag in by_tag), None)
    if revenue is None or cost is None or len(revenue.source_tags) != 1:
        return None
    revenue_source = next(item for item in facts if item.tag == revenue.source_tags[0])
    return _from_sources(
        chain,
        [revenue_source, cost],
        value=revenue.value - cost.value,
        derivation="difference:revenue-cost_of_revenue",
    )


def _custom_revenue_tags(presentation: Sequence[PresentationRow]) -> dict[str, set[str]]:
    candidates: dict[tuple[str, int], list[PresentationRow]] = defaultdict(list)
    for row in presentation:
        if (
            row.stmt == "IS"
            and row.version == row.adsh
            and row.line <= 5
            and _CUSTOM_LABEL.search(row.plabel)
            and not _CUSTOM_REJECT.search(row.plabel)
        ):
            candidates[(row.adsh, row.report)].append(row)
    by_adsh: dict[str, set[str]] = defaultdict(set)
    for (adsh, _), rows in candidates.items():
        candidate_tags = {item.tag for item in rows}
        if len(candidate_tags) == 1:
            by_adsh[adsh].update(candidate_tags)
    return by_adsh


def standardize_facts(
    facts: Iterable[RawFact],
    *,
    presentation: Sequence[PresentationRow] = (),
) -> tuple[StandardizedFact, ...]:
    """Standardize PIT facts without inventing values or using excluded filer forms."""
    grouped: dict[tuple[int, date, int, str], list[RawFact]] = defaultdict(list)
    for fact in facts:
        if fact.accepted.tzinfo is None:
            raise StandardizationError("raw facts require timezone-aware acceptance times")
        if fact.form not in EXCLUDED_FORMS:
            grouped[_same_period_key(fact)].append(fact)

    custom_by_adsh = _custom_revenue_tags(presentation)
    output: list[StandardizedFact] = []
    for key in sorted(grouped):
        period_facts = grouped[key]
        period_output: dict[str, StandardizedFact] = {}
        for chain in CONCEPT_CHAINS:
            selected = _select_chain_fact(chain, period_facts)
            if selected is not None:
                period_output[chain.concept] = selected
        if "gross_profit" not in period_output:
            derived = _derive_gross_profit(period_facts)
            if derived is not None:
                period_output["gross_profit"] = derived
        if "revenue" not in period_output:
            chain = CHAIN_BY_CONCEPT["revenue"]
            candidates = [
                fact
                for fact in period_facts
                if fact.tag in custom_by_adsh.get(fact.adsh, set()) and _valid_unit(chain, fact.uom)
            ]
            candidate_tags = {fact.tag for fact in candidates}
            if len(candidate_tags) == 1 and candidates:
                selected = min(candidates, key=lambda item: (item.accepted, item.adsh))
                period_output["revenue"] = _from_sources(
                    chain,
                    [selected],
                    value=selected.value,
                    derivation="custom-pre:is-top-line-revenue",
                )
        output.extend(period_output.values())
    return tuple(
        sorted(output, key=lambda item: (item.cik, item.ddate, item.qtrs, item.uom, item.concept))
    )


def build_coverage_report(
    facts: Iterable[StandardizedFact],
    *,
    eligible_ciks: Iterable[int],
    sample_quarter: str,
    inputs: Iterable[CoverageInput] = (),
) -> CoverageReport:
    """Diagnose strict observed coverage over the complete v1 concept catalog."""
    eligible = frozenset(eligible_ciks)
    observed: dict[str, set[int]] = defaultdict(set)
    for fact in facts:
        if fact.cik in eligible:
            observed[fact.concept].add(fact.cik)
    concepts = tuple(
        ConceptCoverage(chain.concept, chain.tier, len(observed[chain.concept]), len(eligible))
        for chain in CONCEPT_CHAINS
    )

    def tier_rate(tier: Tier) -> float:
        selected = [item for item in concepts if item.tier == tier]
        denominator = len(selected) * len(eligible)
        if not denominator:
            return 0.0
        return sum(item.issuers_with_value for item in selected) / denominator

    return CoverageReport(
        sample_quarter=sample_quarter,
        chain_version=CHAIN_VERSION,
        eligible_issuers=len(eligible),
        concepts=concepts,
        core_rate=tier_rate("core"),
        secondary_rate=tier_rate("secondary"),
        inputs=tuple(inputs),
    )


def coverage_input(path: str | Path, *, role: str) -> CoverageInput:
    """Hash one Parquet input so a coverage report names its exact evidence."""
    resolved = Path(path)
    if not resolved.is_file():
        raise StandardizationError("coverage evidence input does not exist")
    digest = hashlib.sha256()
    byte_count = 0
    with resolved.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            byte_count += len(chunk)
    try:
        row_count = pq.ParquetFile(resolved).metadata.num_rows
    except OSError as exc:
        raise StandardizationError("coverage evidence input is not readable Parquet") from exc
    return CoverageInput(role, digest.hexdigest(), byte_count, row_count)


def _paths(values: Iterable[str | Path], label: str) -> tuple[Path, ...]:
    paths = tuple(Path(value) for value in values)
    if not paths or any(not path.is_file() for path in paths):
        raise StandardizationError(f"{label} requires existing Parquet files")
    return paths


def standardize_pit_snapshot(
    facts_path: str | Path,
    *,
    presentation_paths: Iterable[str | Path] = (),
) -> tuple[StandardizedFact, ...]:
    """Load only relevant columns/tags from a verified PIT snapshot and standardize them."""
    from usinv.data.edgar.pit_store import PIT_FACT_SCHEMA

    resolved_facts = Path(facts_path)
    if not resolved_facts.is_file():
        raise StandardizationError("facts_pit artifact does not exist")
    try:
        schema = pq.ParquetFile(resolved_facts).schema_arrow
    except OSError as exc:
        raise StandardizationError("facts_pit artifact is not readable Parquet") from exc
    if not schema.equals(PIT_FACT_SCHEMA, check_metadata=True):
        raise StandardizationError("standardization requires a verified facts_pit artifact")

    pre_paths = tuple(Path(value) for value in presentation_paths)
    if any(not path.is_file() for path in pre_paths):
        raise StandardizationError("presentation Parquet artifact does not exist")
    known_tags = sorted(
        {tag for chain in CONCEPT_CHAINS for tag in chain.tags} | set(_COST_OF_REVENUE_TAGS)
    )
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.from_parquet(str(resolved_facts)).create_view("pit_facts")
        known_rows = connection.execute(
            """
            SELECT cik, tag, ddate, qtrs, uom, value, accepted, adsh, version,
                   form, filed, filing_period
            FROM pit_facts
            WHERE value IS NOT NULL AND tag IN (SELECT UNNEST(?))
              AND form NOT IN (SELECT UNNEST(?))
            """,
            [known_tags, sorted(EXCLUDED_FORMS)],
        ).fetchall()
        presentation: list[PresentationRow] = []
        custom_rows: list[tuple[object, ...]] = []
        if pre_paths:
            connection.from_parquet([str(path) for path in pre_paths]).create_view("pre_rows")
            custom_rows = connection.execute(
                """
                SELECT DISTINCT p.cik, p.tag, p.ddate, p.qtrs, p.uom, p.value,
                       p.accepted, p.adsh, p.version, p.form, p.filed, p.filing_period
                FROM pit_facts p
                JOIN pre_rows r USING (adsh, tag, version)
                WHERE p.value IS NOT NULL AND p.version = p.adsh
                  AND p.form NOT IN (SELECT UNNEST(?)) AND r.stmt = 'IS'
                  AND TRY_CAST(r.line AS INTEGER) BETWEEN 1 AND 5
                  AND regexp_matches(r.plabel, '(?i)(revenue|revenues|sales)')
                  AND NOT regexp_matches(
                      r.plabel,
                      '(?i)(cost|segment|geograph|product|customer|related part|per share)'
                  )
                """,
                [sorted(EXCLUDED_FORMS)],
            ).fetchall()
            presentation_rows = connection.execute(
                """
                SELECT DISTINCT adsh, TRY_CAST(report AS INTEGER), TRY_CAST(line AS INTEGER),
                                stmt, tag, version, plabel
                FROM pre_rows
                WHERE version = adsh AND stmt = 'IS'
                  AND TRY_CAST(line AS INTEGER) BETWEEN 1 AND 5
                  AND regexp_matches(plabel, '(?i)(revenue|revenues|sales)')
                  AND NOT regexp_matches(
                      plabel,
                      '(?i)(cost|segment|geograph|product|customer|related part|per share)'
                  )
                """
            ).fetchall()
            presentation = [PresentationRow(*row) for row in presentation_rows]
    except duckdb.Error as exc:
        raise StandardizationError("DuckDB could not read PIT standardization inputs") from exc
    finally:
        connection.close()

    raw_facts = {RawFact(*row) for row in [*known_rows, *custom_rows]}
    return standardize_facts(raw_facts, presentation=presentation)


STRUCTURAL_IDENTITY_TAGS: Final = (
    "CostsAndExpenses",
    "Liabilities",
    "LiabilitiesAndStockholdersEquity",
    "LiabilitiesCurrent",
    "OperatingExpenses",
    "OperatingIncomeLoss",
    "PreferredStockSharesIssued",
    "PreferredStockSharesOutstanding",
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)


def load_structural_identity_facts(facts_path: str | Path) -> tuple[RawFact, ...]:
    """Load only balance-sheet identity tags from a verified PIT snapshot."""
    from usinv.data.edgar.pit_store import PIT_FACT_SCHEMA

    resolved = Path(facts_path)
    if not resolved.is_file():
        raise StandardizationError("facts_pit artifact does not exist")
    try:
        schema = pq.ParquetFile(resolved).schema_arrow
    except OSError as exc:
        raise StandardizationError("facts_pit artifact is not readable Parquet") from exc
    if not schema.equals(PIT_FACT_SCHEMA, check_metadata=True):
        raise StandardizationError(
            "structural identity facts require a verified facts_pit artifact"
        )
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.from_parquet(str(resolved)).create_view("pit_facts")
        rows = connection.execute(
            """
            SELECT cik, tag, ddate, qtrs, uom, value, accepted, adsh, version,
                   form, filed, filing_period
            FROM pit_facts
            WHERE value IS NOT NULL AND qtrs BETWEEN 0 AND 4
              AND tag IN (SELECT UNNEST(?))
              AND form NOT IN (SELECT UNNEST(?))
            """,
            [sorted(STRUCTURAL_IDENTITY_TAGS), sorted(EXCLUDED_FORMS)],
        ).fetchall()
    except duckdb.Error as exc:
        raise StandardizationError("DuckDB could not read structural identity inputs") from exc
    finally:
        connection.close()
    return tuple(
        sorted(
            (RawFact(*row) for row in rows),
            key=lambda item: (item.cik, item.ddate, item.uom, item.tag, item.adsh),
        )
    )


def eligible_ciks_from_filings(filings_paths: Iterable[str | Path]) -> frozenset[int]:
    """Return the v1 domestic, non-excluded SEC filer denominator for a sample quarter."""
    from usinv.data.edgar.fsds import FILINGS_SCHEMA

    paths = _paths(filings_paths, "eligible CIK selection")
    for path in paths:
        if not pq.ParquetFile(path).schema_arrow.equals(FILINGS_SCHEMA, check_metadata=True):
            raise StandardizationError("eligible CIK selection requires verified filings artifacts")
    connection = duckdb.connect(database=":memory:")
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT cik
            FROM read_parquet(?)
            WHERE form IN ('10-K', '10-K/A', '10-Q', '10-Q/A')
              AND countryinc = 'US'
              AND sic NOT BETWEEN 6020 AND 6036
              AND sic NOT BETWEEN 6311 AND 6399
              AND sic NOT IN (6199, 6211, 6798, 2834, 2836, 8731, 6770)
            """,
            [[str(path) for path in paths]],
        ).fetchall()
    except duckdb.Error as exc:
        raise StandardizationError("DuckDB could not select eligible sample filers") from exc
    finally:
        connection.close()
    return frozenset(row[0] for row in rows)
