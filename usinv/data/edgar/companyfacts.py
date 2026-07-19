"""Company Facts cross-check adapter joined to submissions acceptance evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from usinv.data.edgar.client import EdgarDocument, EdgarPayloadError
from usinv.data.edgar.periods import fsds_period
from usinv.data.edgar.submissions import SubmissionFiling, acceptance_by_accession
from usinv.data.edgar.tag_chains import RawFact


@dataclass(frozen=True, slots=True)
class CompanyFactIssue:
    kind: str
    taxonomy: str
    tag: str
    unit: str
    accession: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class CompanyFactsResult:
    cik: int
    entity_name: str
    facts: tuple[RawFact, ...]
    issues: tuple[CompanyFactIssue, ...]
    source_url: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class ParityMismatch:
    key: tuple[int, str, date, int, str]
    edge_value: Decimal | None
    fsds_value: Decimal | None
    reason: str


@dataclass(frozen=True, slots=True)
class ParityReport:
    compared_keys: int
    matched_keys: int
    mismatches: tuple[ParityMismatch, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EdgarPayloadError(f"companyfacts field {field} must be an object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EdgarPayloadError(f"companyfacts field {field} must be an array")
    return value


def _iso_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise EdgarPayloadError(f"companyfacts field {field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EdgarPayloadError(f"companyfacts field {field} must be an ISO date") from exc


def _value(value: object) -> Decimal:
    if isinstance(value, float):
        raise EdgarPayloadError("companyfacts float lost decimal precision before parsing")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EdgarPayloadError("companyfacts val is not decimal") from exc
    if not result.is_finite():
        raise EdgarPayloadError("companyfacts val must be finite")
    return result


def parse_companyfacts_document(
    document: EdgarDocument,
    *,
    filings: Sequence[SubmissionFiling],
) -> CompanyFactsResult:
    """Normalize standard facts; acceptance always comes from an accession join."""
    payload = document.payload
    try:
        cik = int(str(payload.get("cik")))
    except (TypeError, ValueError) as exc:
        raise EdgarPayloadError("companyfacts cik is invalid") from exc
    entity_name = payload.get("entityName")
    if cik <= 0 or not isinstance(entity_name, str) or not entity_name.strip():
        raise EdgarPayloadError("companyfacts entity identity is invalid")
    by_accession = acceptance_by_accession(filings)
    facts_node = _mapping(payload.get("facts"), "facts")
    output: dict[tuple[int, str, date, int, str, str], RawFact] = {}
    conflicted_keys: set[tuple[int, str, date, int, str, str]] = set()
    issues: list[CompanyFactIssue] = []

    for taxonomy, taxonomy_value in sorted(facts_node.items()):
        concepts = _mapping(taxonomy_value, f"facts.{taxonomy}")
        for tag, concept_value in sorted(concepts.items()):
            concept = _mapping(concept_value, f"facts.{taxonomy}.{tag}")
            units = _mapping(concept.get("units"), f"facts.{taxonomy}.{tag}.units")
            for unit, fact_values in sorted(units.items()):
                for fact_value in _sequence(fact_values, f"units.{unit}"):
                    row = _mapping(fact_value, f"units.{unit}[]")
                    accession_value = row.get("accn")
                    accession = accession_value if isinstance(accession_value, str) else None
                    filing = by_accession.get(accession or "")
                    if filing is None:
                        issues.append(
                            CompanyFactIssue(
                                "missing_acceptance_join",
                                taxonomy,
                                tag,
                                unit,
                                accession,
                                "companyfacts filed date is not a PIT timestamp",
                            )
                        )
                        continue
                    if filing.cik != cik:
                        raise EdgarPayloadError("companyfacts accession joined to a different CIK")
                    form = row.get("form")
                    if not isinstance(form, str) or form != filing.form:
                        issues.append(
                            CompanyFactIssue(
                                "form_mismatch",
                                taxonomy,
                                tag,
                                unit,
                                accession,
                                "companyfacts form does not match submissions",
                            )
                        )
                        continue
                    end = _iso_date(row.get("end"), "end")
                    start_value = row.get("start")
                    start = _iso_date(start_value, "start") if start_value is not None else None
                    try:
                        ddate, qtrs = fsds_period(start, end)
                        decimal_value = _value(row.get("val"))
                    except EdgarPayloadError as exc:
                        issues.append(
                            CompanyFactIssue(
                                "invalid_fact",
                                taxonomy,
                                tag,
                                unit,
                                accession,
                                str(exc),
                            )
                        )
                        continue
                    raw = RawFact(
                        cik=cik,
                        tag=tag,
                        ddate=ddate,
                        qtrs=qtrs,
                        uom=unit,
                        value=decimal_value,
                        accepted=filing.accepted,
                        adsh=filing.accession,
                        version=taxonomy,
                        form=filing.form,
                        filed=filing.filing_date,
                        filing_period=filing.report_date,
                    )
                    key = (cik, tag, ddate, qtrs, unit, filing.accession)
                    if key in conflicted_keys:
                        continue
                    prior = output.get(key)
                    if prior is not None and prior.value != raw.value:
                        issues.append(
                            CompanyFactIssue(
                                "same_accession_conflict",
                                taxonomy,
                                tag,
                                unit,
                                accession,
                                "one accession supplies conflicting values for a canonical key",
                            )
                        )
                        output.pop(key, None)
                        conflicted_keys.add(key)
                        continue
                    output[key] = raw
    return CompanyFactsResult(
        cik,
        entity_name.strip(),
        tuple(
            sorted(
                output.values(),
                key=lambda item: (
                    item.cik,
                    item.tag,
                    item.ddate,
                    item.qtrs,
                    item.uom,
                    item.accepted,
                ),
            )
        ),
        tuple(issues),
        document.url,
        document.content_sha256,
    )


def _pit_values(facts: Sequence[RawFact]) -> dict[tuple[int, str, date, int, str], RawFact]:
    result: dict[tuple[int, str, date, int, str], RawFact] = {}
    conflicts: set[tuple[int, str, date, int, str]] = set()
    for fact in facts:
        key = (fact.cik, fact.tag, fact.ddate, fact.qtrs, fact.uom)
        prior = result.get(key)
        if prior is None or (fact.accepted, fact.adsh) < (prior.accepted, prior.adsh):
            result[key] = fact
        elif fact.accepted == prior.accepted and fact.value != prior.value:
            conflicts.add(key)
    for key in conflicts:
        result.pop(key, None)
    return result


def compare_edge_to_fsds(
    edge_facts: Sequence[RawFact],
    fsds_facts: Sequence[RawFact],
) -> ParityReport:
    """Diff canonical first-accepted keys for a retrospective published sample."""
    edge = _pit_values(edge_facts)
    fsds = _pit_values(fsds_facts)
    mismatches: list[ParityMismatch] = []
    for key in sorted(set(edge) | set(fsds)):
        left = edge.get(key)
        right = fsds.get(key)
        if left is None:
            mismatches.append(ParityMismatch(key, None, right.value, "missing_edge"))
        elif right is None:
            mismatches.append(ParityMismatch(key, left.value, None, "missing_fsds"))
        elif left.value != right.value:
            mismatches.append(ParityMismatch(key, left.value, right.value, "value_mismatch"))
    compared = len(set(edge) | set(fsds))
    return ParityReport(compared, compared - len(mismatches), tuple(mismatches))
