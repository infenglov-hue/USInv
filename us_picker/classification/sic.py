"""SEC SIC code -> company type and custom sub-sector (US replacement for the
BIST sector-name keyword rules).

The company type decides which scoring model runs (see company_type.py):
BANK / INSURANCE / REIT / FINANCIAL get the sector models, everything else is
OPERATING.  Codes that are not operating businesses at all (blank-check
SPACs, funds, commodity trusts) return ``EXCLUDE`` and never enter the
universe.
"""

from __future__ import annotations

from typing import Optional

EXCLUDED_TYPE = "EXCLUDE"

# Inclusive SIC ranges, first match wins.
_TYPE_RULES: tuple[tuple[int, int, str], ...] = (
    (6770, 6770, EXCLUDED_TYPE),  # blank checks / SPACs
    (6720, 6729, EXCLUDED_TYPE),  # investment offices, trusts, funds
    (6221, 6221, EXCLUDED_TYPE),  # commodity contracts dealers (commodity ETFs)
    (6189, 6189, EXCLUDED_TYPE),  # asset-backed securities issuers
    (6020, 6036, "BANK"),
    (6211, 6211, "FINANCIAL"),  # broker-dealers (bank-like balance sheets)
    (6099, 6199, "FINANCIAL"),  # credit institutions, consumer/commercial finance
    (6311, 6399, "INSURANCE"),
    (6798, 6798, "REIT"),
)

# (lo, hi, sub_sector); more specific ranges come first.
_SECTOR_RULES: tuple[tuple[int, int, str], ...] = (
    (1311, 1389, "energy_oil_gas"),
    (1000, 1499, "mining"),
    (1500, 1799, "construction"),
    (2080, 2086, "food_production"),
    (2000, 2199, "food_production"),
    (2200, 2399, "textiles"),
    (2400, 2599, "retail_specialty"),
    (2600, 2699, "paper"),
    (2700, 2799, "media"),
    (2830, 2836, "healthcare_pharma"),
    (2800, 2899, "chemicals"),
    (2900, 2999, "energy_oil_gas"),
    (3000, 3099, "chemicals"),
    (3100, 3199, "textiles"),
    (3210, 3231, "glass"),
    (3200, 3299, "cement"),
    (3300, 3399, "steel"),
    (3400, 3499, "industrial_manufacturing"),
    (3570, 3579, "technology_hardware"),
    (3500, 3599, "industrial_manufacturing"),
    (3674, 3674, "technology_semis"),
    (3630, 3639, "retail_general"),
    (3650, 3679, "technology_hardware"),
    (3600, 3699, "industrial_manufacturing"),
    (3710, 3716, "automotive"),
    (3720, 3729, "defense"),
    (3730, 3732, "defense"),
    (3760, 3769, "defense"),
    (3812, 3812, "defense"),
    (3700, 3799, "industrial_manufacturing"),
    (3841, 3851, "healthcare_devices"),
    (3800, 3899, "technology_hardware"),
    (3900, 3999, "industrial_manufacturing"),
    (4512, 4522, "airlines"),
    (4000, 4799, "logistics"),
    (4830, 4841, "media"),
    (4800, 4899, "telecom"),
    (4900, 4999, "energy_power"),
    (5000, 5199, "wholesale"),
    (5400, 5499, "food_retail"),
    (5810, 5813, "restaurants"),
    (5300, 5399, "retail_general"),
    (5961, 5961, "retail_general"),
    (5200, 5999, "retail_specialty"),
    (6020, 6036, "banking_private"),
    (6311, 6399, "insurance"),
    (6798, 6798, "real_estate"),
    (6500, 6553, "real_estate"),
    (6000, 6799, "financial_services"),
    (7000, 7099, "tourism_hotels"),
    (7370, 7379, "technology_software"),
    (7380, 7389, "business_services"),
    (7800, 7849, "media"),
    (7900, 7999, "tourism_hotels"),
    (8000, 8099, "healthcare_services"),
    (8700, 8799, "business_services"),
)


def _code(sic: object) -> Optional[int]:
    try:
        value = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def company_type_for_sic(sic: object) -> str:
    """OPERATING unless the SIC code marks a financial model or exclusion."""

    code = _code(sic)
    if code is None:
        return "OPERATING"
    for lo, hi, company_type in _TYPE_RULES:
        if lo <= code <= hi:
            return company_type
    return "OPERATING"


def sub_sector_for_sic(sic: object) -> str:
    code = _code(sic)
    if code is None:
        return "other"
    for lo, hi, sector in _SECTOR_RULES:
        if lo <= code <= hi:
            return sector
    return "other"


def is_excluded_sic(sic: object) -> bool:
    return company_type_for_sic(sic) == EXCLUDED_TYPE
