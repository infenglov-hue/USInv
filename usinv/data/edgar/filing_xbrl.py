"""As-filed XBRL/iXBRL archive and provenance-preserving parser."""

from __future__ import annotations

import hashlib
import html.entities
import json
import re
import shutil
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final

from lxml import etree

from usinv.data.edgar.client import EdgarClient, EdgarPayloadError, EdgarResource
from usinv.data.edgar.periods import fsds_period
from usinv.data.edgar.securities import (
    Security,
    SecurityMasterError,
    SymbolInterval,
    is_explicit_non_common_security_title,
    mint_security_id,
    normalize_exchange,
    normalize_ticker,
)
from usinv.data.edgar.submissions import SubmissionFiling
from usinv.data.edgar.tag_chains import PresentationRow, RawFact

FILING_XBRL_VERSION: Final = "usinv-filing-xbrl-v1"
_STANDARD_PREFIXES: Final = frozenset(
    {"us-gaap", "dei", "srt", "ifrs-full", "invest", "country", "currency", "exch"}
)
_XLINK: Final = "{http://www.w3.org/1999/xlink}"
_XSI_NIL: Final = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_NUMBER_WORDS: Final = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_SCALES: Final = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_COVER_IDENTITY_TAGS: Final = frozenset(
    {
        "TradingSymbol",
        "EntityTradingSymbol",
        "SecurityExchangeName",
        "Security12bTitle",
        "TitleOf12bSecurity",
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
    }
)


@dataclass(frozen=True, slots=True)
class FilingXbrlIssue:
    kind: str
    context_id: str | None
    tag: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class FilingFact:
    cik: int
    accession: str
    tag: str
    taxonomy: str
    custom: bool
    context_id: str
    period_start: date | None
    period_end: date
    ddate: date
    qtrs: int
    unit: str | None
    decimals: str | None
    value: Decimal | None
    text_value: str | None
    dimensions: tuple[tuple[str, str], ...]
    accepted: datetime
    form: str
    filed: date
    filing_period: date | None
    source_document: str
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class FilingParseResult:
    facts: tuple[FilingFact, ...]
    issues: tuple[FilingXbrlIssue, ...]


@dataclass(frozen=True, slots=True)
class CoverSecurityClass:
    context_id: str
    ticker: str
    exchange: str
    class_title: str
    dimensions: tuple[tuple[str, str], ...]
    evidence_pointers: tuple[str, ...]
    shares_outstanding: Decimal | None
    shares_evidence_pointer: str | None
    exchange_from_allowed_pair: bool = False


@dataclass(frozen=True, slots=True)
class FilingArchiveResource:
    name: str
    url: str
    content_sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class FilingArchiveResult:
    accession: str
    snapshot_id: str
    output_dir: Path
    resources: tuple[FilingArchiveResource, ...]
    from_cache: bool


@dataclass(frozen=True, slots=True)
class _Context:
    context_id: str
    start: date | None
    end: date
    dimensions: tuple[tuple[str, str], ...]


def _local(element_or_tag: etree._Element | str) -> str:
    tag = element_or_tag.tag if isinstance(element_or_tag, etree._Element) else element_or_tag
    return etree.QName(tag).localname if isinstance(tag, str) and tag.startswith("{") else str(tag)


def _safe_xml(body: bytes) -> etree._Element:
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise EdgarPayloadError("filing XBRL is not safe, well-formed XML/XHTML")
    xml_entities = {b"amp", b"apos", b"gt", b"lt", b"quot"}

    def replace_html_entity(match: re.Match[bytes]) -> bytes:
        name = match.group(1)
        if name in xml_entities:
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name.decode("ascii"))
        return f"&#{codepoint};".encode() if codepoint is not None else match.group(0)

    # SEC inline filings are XHTML in practice, but some use named HTML
    # entities such as &nbsp; without a DTD.  Convert only Python's fixed,
    # local entity table to numeric references; external entities stay banned.
    body = re.sub(rb"&([A-Za-z][A-Za-z0-9]+);", replace_html_entity, body)
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=True,
    )
    try:
        return etree.fromstring(body, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise EdgarPayloadError("filing XBRL is not safe, well-formed XML/XHTML") from exc


def _iso_date(text: str | None, field: str) -> date:
    if text is None:
        raise EdgarPayloadError(f"XBRL context is missing {field}")
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise EdgarPayloadError(f"XBRL context {field} is not an ISO date") from exc


def _contexts(root: etree._Element, cik: int) -> tuple[dict[str, _Context], list[FilingXbrlIssue]]:
    output: dict[str, _Context] = {}
    conflicted_contexts: set[str] = set()
    issues: list[FilingXbrlIssue] = []
    for node in root.iter():
        if _local(node) != "context":
            continue
        context_id = node.get("id")
        if not context_id:
            issues.append(FilingXbrlIssue("invalid_context", None, None, "context has no id"))
            continue
        if context_id in conflicted_contexts:
            continue
        identifiers = [
            "".join(child.itertext()).strip()
            for child in node.iter()
            if _local(child) == "identifier"
        ]
        numeric_identifiers = {int(value) for value in identifiers if value.isdigit()}
        if numeric_identifiers != {cik}:
            issues.append(
                FilingXbrlIssue(
                    "entity_mismatch",
                    context_id,
                    None,
                    "context must contain exactly the filing CIK as its numeric entity identifier",
                )
            )
            continue
        instant = next((child.text for child in node.iter() if _local(child) == "instant"), None)
        start_text = next(
            (child.text for child in node.iter() if _local(child) == "startDate"), None
        )
        end_text = next((child.text for child in node.iter() if _local(child) == "endDate"), None)
        try:
            start = None if instant is not None else _iso_date(start_text, "startDate")
            end = _iso_date(instant or end_text, "instant/endDate")
        except EdgarPayloadError as exc:
            issues.append(FilingXbrlIssue("invalid_context", context_id, None, str(exc)))
            continue
        dimensions: list[tuple[str, str]] = []
        for child in node.iter():
            local = _local(child)
            if local == "explicitMember":
                dimensions.append((child.get("dimension", ""), "".join(child.itertext()).strip()))
            elif local == "typedMember":
                value = " ".join("".join(child.itertext()).split())
                dimensions.append((child.get("dimension", ""), value))
        context = _Context(context_id, start, end, tuple(sorted(dimensions)))
        prior = output.get(context_id)
        if prior is not None and prior != context:
            issues.append(
                FilingXbrlIssue(
                    "context_conflict",
                    context_id,
                    None,
                    "one context id has conflicting definitions",
                )
            )
            output.pop(context_id, None)
            conflicted_contexts.add(context_id)
            continue
        output[context_id] = context
    return output, issues


def _measure_name(text: str) -> str:
    value = text.strip()
    if ":" in value:
        value = value.rsplit(":", 1)[1]
    return value


def _units(root: etree._Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in root.iter():
        if _local(node) != "unit" or not node.get("id"):
            continue
        unit_id = node.get("id")
        numerator: list[str] = []
        denominator: list[str] = []
        for child in node.iter():
            if _local(child) != "measure" or not child.text:
                continue
            ancestors = {_local(parent) for parent in child.iterancestors()}
            target = denominator if "unitDenominator" in ancestors else numerator
            target.append(_measure_name(child.text))
        if numerator:
            value = "*".join(numerator)
            if denominator:
                value = f"{value}/{'*'.join(denominator)}"
            result[unit_id] = value
    return result


def _taxonomy_name(element: etree._Element, raw_name: str | None) -> tuple[str, str, bool]:
    if raw_name:
        if raw_name.startswith("{"):
            qname = etree.QName(raw_name)
            namespace = qname.namespace or ""
            prefix = next(
                (key for key, value in element.nsmap.items() if value == namespace and key),
                namespace,
            )
            return qname.localname, prefix, prefix not in _STANDARD_PREFIXES
        if ":" in raw_name:
            prefix, tag = raw_name.split(":", 1)
            return tag, prefix, prefix not in _STANDARD_PREFIXES
        return raw_name, "unknown", True
    qname = etree.QName(element.tag)
    namespace = qname.namespace or ""
    prefix = element.prefix or namespace
    return qname.localname, prefix, prefix not in _STANDARD_PREFIXES


def _continuations(root: etree._Element) -> dict[str, etree._Element]:
    return {
        node.get("id"): node
        for node in root.iter()
        if _local(node) == "continuation" and node.get("id")
    }


def _content(
    node: etree._Element,
    continuations: Mapping[str, etree._Element],
) -> str:
    values: list[str] = []
    seen: set[str] = set()
    current: etree._Element | None = node
    while current is not None:
        values.append(" ".join("".join(current.itertext()).split()))
        next_id = current.get("continuedAt")
        if not next_id or next_id in seen:
            break
        seen.add(next_id)
        current = continuations.get(next_id)
    return " ".join(value for value in values if value).strip()


def _numeric_value(node: etree._Element, text: str) -> Decimal | None:
    if node.get(_XSI_NIL, "false").casefold() == "true":
        return None
    raw = text.replace("\u00a0", " ").strip()
    if raw in {"", "-", "\u2014", "\u2013"}:
        return None
    negative_parentheses = raw.startswith("(") and raw.endswith(")")
    if negative_parentheses:
        raw = raw[1:-1]
    raw = raw.replace("$", "").replace(" ", "")
    format_name = node.get("format", "").casefold()
    if "numwordsen" in format_name:
        words = text.casefold().replace("-", " ").split()
        total = 0
        current = 0
        for word in words:
            if word == "and":
                continue
            if word in _NUMBER_WORDS:
                current += _NUMBER_WORDS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            elif word in _NUMBER_SCALES:
                total += max(current, 1) * _NUMBER_SCALES[word]
                current = 0
            else:
                raise EdgarPayloadError(f"unsupported inline number word: {word!r}")
        raw = str(total + current)
    elif "num-comma-decimal" in format_name:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        value = Decimal(raw)
        scale = int(node.get("scale", "0"))
    except (InvalidOperation, ValueError) as exc:
        raise EdgarPayloadError(f"unsupported inline numeric value: {text!r}") from exc
    if not -20 <= scale <= 20:
        raise EdgarPayloadError("inline XBRL scale is outside the supported range")
    value *= Decimal(10) ** scale
    if negative_parentheses:
        value = -value
    if node.get("sign") == "-":
        value = -value
    if not value.is_finite():
        raise EdgarPayloadError("inline XBRL numeric value must be finite")
    return value


def _decimals(node: etree._Element) -> str | None:
    value = node.get("decimals")
    if value is None:
        return None
    if value == "INF":
        return value
    try:
        parsed = int(value)
    except ValueError as exc:
        raise EdgarPayloadError(f"invalid XBRL decimals value: {value!r}") from exc
    if not -100 <= parsed <= 100:
        raise EdgarPayloadError("XBRL decimals is outside the supported range")
    return str(parsed)


def parse_filing_xbrl(
    body: bytes,
    *,
    filing: SubmissionFiling,
    source_document: str,
) -> FilingParseResult:
    """Parse numeric and cover facts from one safe as-filed XBRL/iXBRL document."""
    root = _safe_xml(body)
    contexts, issues = _contexts(root, filing.cik)
    units = _units(root)
    continuations = _continuations(root)
    output: list[FilingFact] = []
    for node in root.iter():
        context_id = node.get("contextRef")
        if not context_id:
            continue
        context = contexts.get(context_id)
        if context is None:
            issues.append(
                FilingXbrlIssue(
                    "missing_context",
                    context_id,
                    None,
                    "fact references an absent or invalid context",
                )
            )
            continue
        local = _local(node)
        if local in {"fraction", "tuple"}:
            issues.append(
                FilingXbrlIssue(
                    "unsupported_fact_type",
                    context_id,
                    None,
                    f"{local} facts are quarantined",
                )
            )
            continue
        inline = local in {"nonFraction", "nonNumeric"}
        raw_name = node.get("name") if inline else None
        tag, taxonomy, custom = _taxonomy_name(node, raw_name)
        try:
            ddate, qtrs = fsds_period(context.start, context.end)
        except EdgarPayloadError as exc:
            if tag in _COVER_IDENTITY_TAGS:
                ddate, qtrs = fsds_period(None, context.end)
            else:
                issues.append(FilingXbrlIssue("invalid_period", context_id, tag, str(exc)))
                continue
        text = _content(node, continuations)
        unit_ref = node.get("unitRef")
        unit = units.get(unit_ref) if unit_ref else None
        if unit_ref and unit is None:
            issues.append(
                FilingXbrlIssue(
                    "missing_unit",
                    context_id,
                    tag,
                    f"fact references unknown unit {unit_ref!r}",
                )
            )
            continue
        numeric = local == "nonFraction" or (not inline and unit_ref is not None)
        try:
            value = _numeric_value(node, text) if numeric else None
            decimals = _decimals(node) if numeric else None
        except EdgarPayloadError as exc:
            issues.append(FilingXbrlIssue("invalid_numeric", context_id, tag, str(exc)))
            continue
        if numeric and value is None:
            continue
        evidence_pointer = (
            f"{filing.source_url}#{filing.accession}/{source_document}/{context_id}/{tag}"
        )
        output.append(
            FilingFact(
                cik=filing.cik,
                accession=filing.accession,
                tag=tag,
                taxonomy=taxonomy,
                custom=custom,
                context_id=context_id,
                period_start=context.start,
                period_end=context.end,
                ddate=ddate,
                qtrs=qtrs,
                unit=unit,
                decimals=decimals,
                value=value,
                text_value=None if numeric else text,
                dimensions=context.dimensions,
                accepted=filing.accepted,
                form=filing.form,
                filed=filing.filing_date,
                filing_period=filing.report_date,
                source_document=source_document,
                evidence_pointer=evidence_pointer,
            )
        )
    return FilingParseResult(
        tuple(
            sorted(
                output,
                key=lambda item: (
                    item.tag,
                    item.ddate,
                    item.qtrs,
                    item.unit or "",
                    item.context_id,
                ),
            )
        ),
        tuple(issues),
    )


def parse_plain_html_cover_table(
    body: bytes,
    *,
    filing: SubmissionFiling,
    source_document: str,
    expected_listing_pair: tuple[str, str] | None = None,
) -> FilingParseResult:
    """Parse an explicit cover table or an exact-pair prospectus listing statement."""

    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise EdgarPayloadError("plain filing HTML contains prohibited DTD markup")
    try:
        root = etree.fromstring(
            body,
            parser=etree.HTMLParser(no_network=True, recover=True),
        )
    except etree.XMLSyntaxError as exc:
        raise EdgarPayloadError("plain filing HTML is not parseable") from exc
    if root is None:
        raise EdgarPayloadError("plain filing HTML is empty")

    def cell_text(node: etree._Element) -> str:
        return " ".join("".join(node.itertext()).replace("\xa0", " ").split())

    def header_key(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    rows: set[tuple[str, str, str]] = set()
    for table in root.xpath(".//table"):
        table_rows: list[tuple[str, ...]] = []
        for table_row in table.xpath(".//tr"):
            cells = tuple(cell_text(cell) for cell in table_row.xpath("./th|./td"))
            if cells:
                table_rows.append(cells)
        for index, header in enumerate(table_rows):
            keys = tuple(header_key(value) for value in header)
            title_index = next(
                (position for position, value in enumerate(keys) if "titleofeachclass" in value),
                None,
            )
            ticker_index = next(
                (position for position, value in enumerate(keys) if "tradingsymbol" in value),
                None,
            )
            exchange_index = next(
                (
                    position
                    for position, value in enumerate(keys)
                    if "nameofeachexchange" in value or "exchangeonwhichregistered" in value
                ),
                None,
            )
            if None in (title_index, ticker_index, exchange_index):
                continue
            assert title_index is not None
            assert ticker_index is not None
            assert exchange_index is not None
            maximum_index = max(title_index, ticker_index, exchange_index)
            for data_row in table_rows[index + 1 : index + 13]:
                if len(data_row) <= maximum_index:
                    continue
                title = data_row[title_index].strip()
                ticker = data_row[ticker_index].strip()
                exchange = data_row[exchange_index].strip()
                if title and ticker and exchange:
                    try:
                        normalize_ticker(ticker)
                        normalize_exchange(exchange)
                    except SecurityMasterError:
                        continue
                    rows.add((title, ticker, exchange))

    if expected_listing_pair is not None and not rows:
        expected_ticker, expected_exchange = expected_listing_pair
        try:
            expected_ticker = normalize_ticker(expected_ticker)
            expected_exchange = normalize_exchange(expected_exchange)
        except SecurityMasterError as exc:
            raise EdgarPayloadError("plain filing expected listing pair is invalid") from exc
        visible_text = " ".join("".join(root.itertext()).replace("\xa0", " ").split())
        ticker_pattern = re.escape(expected_ticker).replace(r"\-", r"[- ]")
        statement_pattern = re.compile(
            r"\b(?:have\s+(?:been\s+)?(?:applied|approved)|(?:have\s+)?applied)"
            r"\s+to\s+list\s+(?:our\s+)?"
            r"(?P<title>.{1,100}?)\s+on\s+"
            r"(?P<exchange>"
            r"(?:the\s+)?New\s+York\s+Stock\s+Exchange|NYSE|"
            r"(?:the\s+)?NYSE\s+American(?:\s+LLC)?|"
            r"(?:the\s+)?Nasdaq(?:\s+Global\s+Select\s+Market|"
            r"\s+Global\s+Market|\s+Capital\s+Market|\s+Stock\s+Market)?)"
            r"(?:\s*\([^)]{1,40}\))?\s+under\s+(?:the\s+)?symbol\s+"
            rf"[\"'“”\u2018\u2019\ufffd]?(?P<ticker>{ticker_pattern})\b",
            re.IGNORECASE,
        )
        statement_rows: set[tuple[str, str, str]] = set()
        for match in statement_pattern.finditer(visible_text):
            title_text = " ".join(match.group("title").split())
            lowered = title_text.casefold()
            if is_explicit_non_common_security_title(title_text) or not any(
                value in lowered
                for value in (
                    "common stock",
                    "common shares",
                    "ordinary shares",
                    "subordinate voting shares",
                )
            ):
                continue
            try:
                observed_exchange = normalize_exchange(match.group("exchange"))
            except SecurityMasterError:
                continue
            if observed_exchange != expected_exchange:
                continue
            if "ordinary shares" in lowered:
                class_title = "Ordinary Shares"
            elif "subordinate voting shares" in lowered:
                class_title = "Subordinate Voting Shares"
            elif "common shares" in lowered:
                class_title = "Common Shares"
            else:
                class_title = "Common Stock"
            statement_rows.add((class_title, expected_ticker, expected_exchange))
        if len(statement_rows) == 1:
            rows.update(statement_rows)

    period_end = filing.report_date or filing.filing_date
    facts: list[FilingFact] = []
    for row_number, (title, ticker, exchange) in enumerate(sorted(rows), start=1):
        context_id = f"plain-html-cover-row-{row_number}"
        for tag, value in (
            ("Security12bTitle", title),
            ("TradingSymbol", ticker),
            ("SecurityExchangeName", exchange),
        ):
            facts.append(
                FilingFact(
                    cik=filing.cik,
                    accession=filing.accession,
                    tag=tag,
                    taxonomy="dei",
                    custom=False,
                    context_id=context_id,
                    period_start=None,
                    period_end=period_end,
                    ddate=period_end,
                    qtrs=0,
                    unit=None,
                    decimals=None,
                    value=None,
                    text_value=value,
                    dimensions=(),
                    accepted=filing.accepted,
                    form=filing.form,
                    filed=filing.filing_date,
                    filing_period=filing.report_date,
                    source_document=source_document,
                    evidence_pointer=(
                        f"{filing.source_url}#{filing.accession}/{source_document}/"
                        f"{context_id}/{tag}"
                    ),
                )
            )
    return FilingParseResult(
        tuple(
            sorted(
                facts,
                key=lambda item: (item.tag, item.context_id),
            )
        ),
        (),
    )


def _precision(fact: FilingFact) -> int:
    if fact.decimals == "INF":
        return 1_000
    if fact.decimals is None:
        return -1_000
    return int(fact.decimals)


def consolidated_filing_facts(
    result: FilingParseResult,
) -> tuple[tuple[FilingFact, ...], tuple[FilingXbrlIssue, ...]]:
    """Coalesce precision-consistent duplicate dimensionless numeric facts."""
    grouped: dict[tuple[int, str, date, int, str], list[FilingFact]] = defaultdict(list)
    for fact in result.facts:
        if fact.value is not None and fact.unit is not None and not fact.dimensions:
            grouped[(fact.cik, fact.tag, fact.ddate, fact.qtrs, fact.unit)].append(fact)
    output: list[FilingFact] = []
    issues: list[FilingXbrlIssue] = []
    for key in sorted(grouped):
        rows = grouped[key]
        selected = max(
            rows,
            key=lambda item: (
                _precision(item),
                item.value,
                item.context_id,
                item.evidence_pointer,
            ),
        )
        consistent = True
        for row in rows:
            if row.value == selected.value:
                continue
            if row.decimals in {None, "INF"}:
                consistent = False
                break
            quantum = Decimal(1).scaleb(-int(row.decimals))
            if selected.value.quantize(quantum) != row.value:
                consistent = False
                break
        if not consistent:
            issues.append(
                FilingXbrlIssue(
                    "canonical_value_conflict",
                    selected.context_id,
                    selected.tag,
                    "duplicate facts are inconsistent at their declared XBRL precision",
                )
            )
            continue
        output.append(selected)
    return tuple(output), tuple(issues)


def filing_facts_to_raw(result: FilingParseResult) -> tuple[RawFact, ...]:
    """Admit only dimensionless numeric facts to the consolidated fundamental spine."""
    consolidated, _ = consolidated_filing_facts(result)
    output = [
        RawFact(
            cik=fact.cik,
            tag=fact.tag,
            ddate=fact.ddate,
            qtrs=fact.qtrs,
            uom=fact.unit,
            value=fact.value,
            accepted=fact.accepted,
            adsh=fact.accession,
            version=fact.accession if fact.custom else fact.taxonomy,
            form=fact.form,
            filed=fact.filed,
            filing_period=fact.filing_period,
        )
        for fact in consolidated
    ]
    return tuple(
        sorted(output, key=lambda item: (item.tag, item.ddate, item.qtrs, item.uom, item.adsh))
    )


def extract_cover_security_classes(
    result: FilingParseResult,
    *,
    allowed_pairs: frozenset[tuple[str, str]] | None = None,
    exact_ticker_evidence_pair: tuple[str, str] | None = None,
) -> tuple[CoverSecurityClass, ...]:
    """Extract filing-time ticker/exchange/class tuples without collapsing dimensions."""

    if exact_ticker_evidence_pair is not None and (
        allowed_pairs is None or exact_ticker_evidence_pair not in allowed_pairs
    ):
        raise EdgarPayloadError(
            "exact filing-index ticker evidence is outside the allowed listing pairs"
        )

    def one_semantic_fact(facts: list[FilingFact]) -> FilingFact | None:
        if not facts:
            return None
        identities = {(" ".join(fact.text_value.split()), fact.dimensions) for fact in facts}
        if len(identities) != 1:
            return None
        return min(facts, key=lambda fact: fact.evidence_pointer)

    by_context: dict[str, dict[str, list[FilingFact]]] = defaultdict(lambda: defaultdict(list))
    for fact in result.facts:
        by_context[fact.context_id][fact.tag].append(fact)
    output: list[CoverSecurityClass] = []
    for context_id, concepts in by_context.items():
        tickers = concepts.get("TradingSymbol", []) + concepts.get("EntityTradingSymbol", [])
        exchanges = concepts.get("SecurityExchangeName", [])
        titles = concepts.get("Security12bTitle", []) + concepts.get("TitleOf12bSecurity", [])
        shares = concepts.get("EntityCommonStockSharesOutstanding", []) + concepts.get(
            "CommonStockSharesOutstanding", []
        )
        ticker = one_semantic_fact(tickers)
        exchange = one_semantic_fact(exchanges)
        title = one_semantic_fact(titles)
        if ticker is None or exchange is None or title is None:
            continue
        facts = (ticker, exchange, title)
        if len({fact.dimensions for fact in facts}) != 1:
            continue
        share_fact = (
            shares[0]
            if len(shares) == 1
            and shares[0].dimensions == ticker.dimensions
            and shares[0].value is not None
            and shares[0].value > 0
            else None
        )
        output.append(
            CoverSecurityClass(
                context_id,
                ticker.text_value,
                exchange.text_value,
                title.text_value,
                ticker.dimensions,
                tuple(sorted(fact.evidence_pointer for fact in (*tickers, *exchanges, *titles))),
                share_fact.value if share_fact else None,
                share_fact.evidence_pointer if share_fact else None,
            )
        )
    if not output:
        # Some SEC filings keep ticker/title in the entity context but put
        # exchanges in EntityListingsExchangeAxis contexts. Admit only the
        # unambiguous single-class case; multiple ticker/title identities stay
        # unresolved.
        all_concepts: dict[str, list[FilingFact]] = defaultdict(list)
        for concepts in by_context.values():
            for tag, facts in concepts.items():
                all_concepts[tag].extend(facts)
        tickers = all_concepts.get("TradingSymbol", []) + all_concepts.get(
            "EntityTradingSymbol", []
        )
        exchanges = all_concepts.get("SecurityExchangeName", [])
        titles = all_concepts.get("Security12bTitle", []) + all_concepts.get(
            "TitleOf12bSecurity", []
        )
        common_shares = all_concepts.get(
            "EntityCommonStockSharesOutstanding",
            [],
        ) + all_concepts.get("CommonStockSharesOutstanding", [])

        def class_dimensions(fact: FilingFact) -> tuple[tuple[str, str], ...]:
            return tuple(
                dimension
                for dimension in fact.dimensions
                if dimension[0].split(":")[-1] != "EntityListingsExchangeAxis"
            )

        def one_class_fact(facts: list[FilingFact]) -> FilingFact | None:
            if not facts:
                return None
            identities = {
                (" ".join(fact.text_value.split()), class_dimensions(fact)) for fact in facts
            }
            if len(identities) != 1:
                return None
            return min(facts, key=lambda fact: fact.evidence_pointer)

        ticker = one_class_fact(tickers)
        title = one_class_fact(titles)
        exchange_groups: dict[str, list[FilingFact]] = defaultdict(list)
        for exchange in exchanges:
            exchange_groups[" ".join(exchange.text_value.split())].append(exchange)
        selected_exchanges = tuple(
            one_class_fact(exchange_groups[value]) for value in sorted(exchange_groups)
        )
        undimensioned_shares = [
            fact
            for fact in common_shares
            if not fact.dimensions and fact.value is not None and fact.value > 0
        ]
        latest_share = None
        if undimensioned_shares:
            latest_period = max(fact.period_end for fact in undimensioned_shares)
            latest_share = min(
                (fact for fact in undimensioned_shares if fact.period_end == latest_period),
                key=lambda fact: fact.evidence_pointer,
            )
        if (
            ticker is not None
            and title is not None
            and selected_exchanges
            and all(exchange is not None for exchange in selected_exchanges)
            and class_dimensions(ticker) == class_dimensions(title)
            and all(
                class_dimensions(exchange) in {(), class_dimensions(ticker)}
                for exchange in selected_exchanges
                if exchange is not None
            )
        ):
            for exchange in selected_exchanges:
                assert exchange is not None
                exchange_facts = exchange_groups[" ".join(exchange.text_value.split())]
                output.append(
                    CoverSecurityClass(
                        ticker.context_id,
                        ticker.text_value,
                        exchange.text_value,
                        title.text_value,
                        class_dimensions(ticker),
                        tuple(
                            sorted(
                                fact.evidence_pointer
                                for fact in (*tickers, *titles, *exchange_facts)
                            )
                        ),
                        None,
                        None,
                    )
                )
        elif (
            ticker is not None
            and title is not None
            and not selected_exchanges
            and allowed_pairs
            and class_dimensions(ticker) == class_dimensions(title)
        ):
            try:
                normalized_ticker = normalize_ticker(ticker.text_value)
            except SecurityMasterError:
                normalized_ticker = ""
            inferred_pairs = tuple(
                sorted(pair for pair in allowed_pairs if pair[0] == normalized_ticker)
            )
            if len(inferred_pairs) == 1:
                output.append(
                    CoverSecurityClass(
                        ticker.context_id,
                        ticker.text_value,
                        inferred_pairs[0][1],
                        title.text_value,
                        class_dimensions(ticker),
                        tuple(sorted(fact.evidence_pointer for fact in (*tickers, *titles))),
                        None,
                        None,
                        True,
                    )
                )
        elif ticker is not None and title is None and latest_share is not None and allowed_pairs:
            try:
                normalized_ticker = normalize_ticker(ticker.text_value)
            except SecurityMasterError:
                normalized_ticker = ""
            inferred_pairs = tuple(
                sorted(pair for pair in allowed_pairs if pair[0] == normalized_ticker)
            )
            exact_exchange = (
                selected_exchanges[0]
                if len(selected_exchanges) == 1
                and selected_exchanges[0] is not None
                and class_dimensions(selected_exchanges[0]) in {(), class_dimensions(ticker)}
                else None
            )
            exchange_text = (
                exact_exchange.text_value
                if exact_exchange is not None
                else inferred_pairs[0][1]
                if not selected_exchanges and len(inferred_pairs) == 1
                else None
            )
            if exchange_text is not None:
                output.append(
                    CoverSecurityClass(
                        ticker.context_id,
                        ticker.text_value,
                        exchange_text,
                        "Common Stock",
                        class_dimensions(ticker),
                        tuple(
                            sorted(
                                {
                                    *(fact.evidence_pointer for fact in tickers),
                                    *(
                                        fact.evidence_pointer
                                        for fact in exchange_groups.get(
                                            " ".join(exchange_text.split()),
                                            (),
                                        )
                                    ),
                                    latest_share.evidence_pointer,
                                }
                            )
                        ),
                        latest_share.value,
                        latest_share.evidence_pointer,
                        exact_exchange is None,
                    )
                )
        elif ticker is None and exact_ticker_evidence_pair is not None:
            inferred_ticker, inferred_exchange = exact_ticker_evidence_pair
            normalized_exchanges: set[str] = set()
            for selected_exchange in selected_exchanges:
                if selected_exchange is None:
                    continue
                try:
                    normalized_exchanges.add(normalize_exchange(selected_exchange.text_value))
                except SecurityMasterError:
                    normalized_exchanges.add("")
            exchange_consistent = not normalized_exchanges or normalized_exchanges == {
                inferred_exchange
            }
            title_text = title.text_value if title is not None else ""
            lowered_title = title_text.casefold()
            explicit_common_title = bool(title_text) and (
                not is_explicit_non_common_security_title(title_text)
                and (
                    "common" in lowered_title
                    or "ordinary share" in lowered_title
                    or "subordinate voting share" in lowered_title
                )
            )
            if exchange_consistent and (explicit_common_title or (title is None and latest_share)):
                anchor = title or latest_share
                assert anchor is not None
                output.append(
                    CoverSecurityClass(
                        anchor.context_id,
                        inferred_ticker,
                        inferred_exchange,
                        title_text or "Common Stock",
                        class_dimensions(title) if title is not None else (),
                        tuple(
                            sorted(
                                {
                                    *(fact.evidence_pointer for fact in titles),
                                    *(fact.evidence_pointer for fact in exchanges),
                                    *(
                                        (latest_share.evidence_pointer,)
                                        if latest_share is not None
                                        else ()
                                    ),
                                }
                            )
                        ),
                        latest_share.value if latest_share is not None else None,
                        latest_share.evidence_pointer if latest_share is not None else None,
                        True,
                    )
                )
    common_indexes = [
        index for index, cover in enumerate(output) if "common" in cover.class_title.casefold()
    ]
    if len(common_indexes) == 1 and output[common_indexes[0]].shares_outstanding is None:
        candidates = [
            fact
            for fact in result.facts
            if fact.tag == "EntityCommonStockSharesOutstanding"
            and not fact.dimensions
            and fact.value is not None
            and fact.value > 0
        ]
        if candidates:
            latest_period = max(fact.period_end for fact in candidates)
            latest = [fact for fact in candidates if fact.period_end == latest_period]
            values = {fact.value for fact in latest}
            if len(values) == 1:
                share_fact = min(latest, key=lambda fact: fact.evidence_pointer)
                index = common_indexes[0]
                output[index] = replace(
                    output[index],
                    shares_outstanding=share_fact.value,
                    shares_evidence_pointer=share_fact.evidence_pointer,
                )
    return tuple(sorted(output, key=lambda item: (item.ticker, item.exchange, item.context_id)))


def security_evidence_from_cover(
    filing: SubmissionFiling,
    cover: CoverSecurityClass,
    *,
    domestic_flag: bool = True,
) -> tuple[Security, SymbolInterval]:
    """Create a conservative class identity and forward-only filing-time symbol interval."""
    title = " ".join(cover.class_title.split())
    lowered = title.casefold()
    if "preferred" in lowered:
        security_type = "preferred_stock"
    elif is_explicit_non_common_security_title(title):
        security_type = "other"
    elif (
        "common" in lowered or "ordinary share" in lowered or "subordinate voting share" in lowered
    ):
        security_type = "common_stock"
    else:
        security_type = "other"
    anchor_body: object = cover.dimensions or lowered
    identity_anchor = "sec-cover-class:" + json.dumps(
        anchor_body,
        sort_keys=True,
        separators=(",", ":"),
    )
    security_id = mint_security_id(filing.cik, identity_anchor)
    evidence_pointer = ";".join(cover.evidence_pointers)
    security = Security(
        security_id=security_id,
        cik=filing.cik,
        class_title=title,
        security_type=security_type,
        domestic_flag=domestic_flag,
        identity_anchor=identity_anchor,
        source="sec_xbrl_cover",
        evidence_pointer=evidence_pointer,
    )
    symbol = SymbolInterval(
        security_id=security_id,
        ticker=normalize_ticker(cover.ticker),
        exchange=normalize_exchange(cover.exchange),
        valid_from=filing.accepted.astimezone(UTC).date(),
        valid_to=None,
        source="sec_xbrl_cover",
        confidence="high",
        evidence_pointer=evidence_pointer,
        known_at=filing.accepted.astimezone(UTC),
        scope="historical_interval",
    )
    return security, symbol


def _fragment_tag(value: str) -> tuple[str, str, bool]:
    fragment = value.rsplit("#", 1)[-1]
    if "_" in fragment:
        prefix, tag = fragment.split("_", 1)
    else:
        prefix, tag = "unknown", fragment
    return tag, prefix, prefix not in _STANDARD_PREFIXES


def _labels(label_root: etree._Element | None) -> dict[str, str]:
    if label_root is None:
        return {}
    result: dict[str, str] = {}
    for link in label_root.iter():
        if _local(link) != "labelLink":
            continue
        locations = {
            child.get(f"{_XLINK}label"): child.get(f"{_XLINK}href")
            for child in link
            if _local(child) == "loc"
        }
        resources = {
            child.get(f"{_XLINK}label"): " ".join("".join(child.itertext()).split())
            for child in link
            if _local(child) == "label"
        }
        for child in link:
            if _local(child) != "labelArc":
                continue
            location = locations.get(child.get(f"{_XLINK}from"))
            label = resources.get(child.get(f"{_XLINK}to"))
            if location and label:
                result[location.rsplit("#", 1)[-1]] = label
    return result


def _statement(role: str) -> str:
    lowered = role.casefold()
    if any(value in lowered for value in ("income", "operation", "earnings")):
        return "IS"
    if "cashflow" in lowered or "cash_flow" in lowered:
        return "CF"
    if any(value in lowered for value in ("balance", "financialposition")):
        return "BS"
    return "UN"


def parse_presentation_linkbase(
    presentation_body: bytes,
    *,
    accession: str,
    label_body: bytes | None = None,
) -> tuple[PresentationRow, ...]:
    """Convert XBRL presentation/label linkbases to conservative PRE-like rows."""
    presentation_root = _safe_xml(presentation_body)
    label_root = _safe_xml(label_body) if label_body else None
    labels = _labels(label_root)
    reports: list[tuple[str, etree._Element]] = [
        (node.get(f"{_XLINK}role", ""), node)
        for node in presentation_root.iter()
        if _local(node) == "presentationLink"
    ]
    output: list[PresentationRow] = []
    for report, (role, link) in enumerate(sorted(reports, key=lambda item: item[0]), start=1):
        locations = {
            child.get(f"{_XLINK}label"): child.get(f"{_XLINK}href")
            for child in link
            if _local(child) == "loc"
        }
        ordered: list[tuple[Decimal, str]] = []
        for child in link:
            if _local(child) != "presentationArc":
                continue
            target = locations.get(child.get(f"{_XLINK}to"))
            if not target:
                continue
            try:
                order = Decimal(child.get("order", "0"))
            except InvalidOperation:
                order = Decimal(0)
            ordered.append((order, target))
        seen: set[str] = set()
        line = 0
        for _, target in sorted(ordered, key=lambda item: (item[0], item[1])):
            fragment = target.rsplit("#", 1)[-1]
            if fragment in seen:
                continue
            seen.add(fragment)
            line += 1
            tag, prefix, custom = _fragment_tag(target)
            output.append(
                PresentationRow(
                    accession,
                    report,
                    line,
                    _statement(role),
                    tag,
                    accession if custom else prefix,
                    labels.get(fragment, tag),
                )
            )
    return tuple(output)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _index_names(resource: EdgarResource) -> tuple[str, ...]:
    try:
        payload = json.loads(resource.body)
        items = payload["directory"]["item"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC filing index.json schema is invalid") from exc
    if not isinstance(items, Sequence):
        raise EdgarPayloadError("SEC filing index items must be an array")
    names: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise EdgarPayloadError("SEC filing index item is invalid")
        name = item["name"]
        if PurePosixPath(name).name != name or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", name
        ):
            raise EdgarPayloadError("SEC filing index contains an unsafe filename")
        names.append(name)
    return tuple(sorted(set(names)))


def _is_instance_candidate(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(".xml") and not re.search(r"_(?:cal|def|lab|pre)\.xml$", lowered)


def _is_complete_submission(name: str, accession: str) -> bool:
    return name.casefold() == f"{accession}.txt".casefold()


def _verify_archive(path: Path, snapshot_id: str) -> tuple[FilingArchiveResource, ...]:
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EdgarPayloadError("filing archive manifest is unreadable") from exc
    if manifest.get("snapshot_id") != snapshot_id:
        raise EdgarPayloadError("filing archive snapshot id mismatch")
    resources: list[FilingArchiveResource] = []
    for item in manifest.get("resources", []):
        resource_path = path / item["name"]
        if not resource_path.is_file() or _sha256_file(resource_path) != item["content_sha256"]:
            raise EdgarPayloadError(f"filing archive resource hash mismatch: {item['name']}")
        resources.append(FilingArchiveResource(**item))
    return tuple(resources)


def _replace_directory_with_retry(
    temporary: Path,
    target: Path,
    *,
    attempts: int = 6,
    initial_delay_seconds: float = 0.05,
) -> None:
    """Publish an archive despite transient Windows scanner/indexer locks."""
    for attempt in range(attempts):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if target.exists():
                return
            if attempt == attempts - 1:
                raise
            time.sleep(initial_delay_seconds * (2**attempt))


def archive_filing(
    client: EdgarClient,
    filing: SubmissionFiling,
    output_root: str | Path,
    *,
    refresh: bool = False,
    include_presentation: bool = False,
    include_filing_header: bool = False,
    reuse_existing: bool = False,
) -> FilingArchiveResult:
    """Archive primary and XBRL data files into an immutable accession snapshot."""
    root = Path(output_root) / "accessions" / filing.accession / "snapshots"
    if reuse_existing and not refresh and not include_presentation and not include_filing_header:
        for target in sorted(
            (path for path in root.glob("*") if path.is_dir()),
            key=lambda path: path.name,
        ):
            try:
                manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                resources = _verify_archive(target, target.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            names = {row.name for row in resources}
            if (
                manifest.get("cik") == filing.cik
                and manifest.get("accession") == filing.accession
                and manifest.get("accepted") == filing.accepted.astimezone(UTC).isoformat()
                and PurePosixPath(filing.primary_document).name in names
                and "index.json" in names
            ):
                return FilingArchiveResult(
                    filing.accession,
                    target.name,
                    target,
                    resources,
                    True,
                )
    index = client.filing_resource(filing.cik, filing.accession, "index.json", refresh=refresh)
    names = _index_names(index)
    primary = PurePosixPath(filing.primary_document).name
    selected = {name for name in names if name == primary or _is_instance_candidate(name)}
    if include_presentation:
        selected.update(
            name for name in names if name.casefold().endswith(("_pre.xml", "_lab.xml"))
        )
    if include_filing_header:
        selected.update(name for name in names if _is_complete_submission(name, filing.accession))
    if primary not in selected:
        raise EdgarPayloadError("filing primary document is absent from its SEC index")
    fetched = [index]
    for name in sorted(selected):
        fetched.append(client.filing_resource(filing.cik, filing.accession, name, refresh=refresh))
    descriptor = {
        "version": FILING_XBRL_VERSION,
        "cik": filing.cik,
        "accession": filing.accession,
        "accepted": filing.accepted.astimezone(UTC).isoformat(),
        "resources": [
            {
                "name": "index.json" if item is index else item.url.rsplit("/", 1)[-1],
                "sha256": item.content_sha256,
            }
            for item in fetched
        ],
    }
    snapshot_id = hashlib.sha256(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = root / snapshot_id
    if target.exists():
        resources = _verify_archive(target, snapshot_id)
        return FilingArchiveResult(filing.accession, snapshot_id, target, resources, True)

    temporary = root / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        resources: list[FilingArchiveResource] = []
        for item in fetched:
            name = "index.json" if item is index else item.url.rsplit("/", 1)[-1]
            path = temporary / name
            path.write_bytes(item.body)
            resources.append(
                FilingArchiveResource(name, item.url, item.content_sha256, len(item.body))
            )
        manifest = {
            "schema_version": 1,
            "parser_version": FILING_XBRL_VERSION,
            "snapshot_id": snapshot_id,
            "cik": filing.cik,
            "accession": filing.accession,
            "form": filing.form,
            "accepted": filing.accepted.astimezone(UTC).isoformat(),
            "resources": [
                {
                    "name": item.name,
                    "url": item.url,
                    "content_sha256": item.content_sha256,
                    "byte_count": item.byte_count,
                }
                for item in resources
            ],
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _replace_directory_with_retry(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verified = _verify_archive(target, snapshot_id)
    return FilingArchiveResult(filing.accession, snapshot_id, target, verified, False)
