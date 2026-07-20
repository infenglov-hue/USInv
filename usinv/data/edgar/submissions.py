"""Schema-strict SEC submissions parsing and PIT-safe periodic filing detection."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from usinv.data.edgar.client import EdgarDocument, EdgarPayloadError

ACCESSION_PATTERN: Final = re.compile(r"^\d{10}-\d{2}-\d{6}$")
PERIODIC_FORMS: Final = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
_REQUIRED_COLUMNS: Final = (
    "accessionNumber",
    "filingDate",
    "acceptanceDateTime",
    "form",
    "primaryDocument",
)


@dataclass(frozen=True, slots=True)
class FormerName:
    name: str
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class CurrentSymbol:
    ticker: str
    exchange: str


@dataclass(frozen=True, slots=True)
class SubmissionFiling:
    cik: int
    accession: str
    form: str
    filing_date: date
    accepted: datetime
    report_date: date | None
    primary_document: str
    source_url: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class SubmissionFormObservation:
    cik: int
    accession: str
    form: str
    accepted: datetime
    source_url: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class SubmissionFeed:
    cik: int
    name: str
    entity_type: str | None
    sic: int | None
    state_of_incorporation: str | None
    current_symbols: tuple[CurrentSymbol, ...]
    former_names: tuple[FormerName, ...]
    filings: tuple[SubmissionFiling, ...]
    history_files: tuple[str, ...]
    observed_at: datetime
    source_url: str
    source_sha256: str
    unusable_filings: int = 0
    unusable_current_symbols: int = 0
    form_history: tuple[SubmissionFormObservation, ...] = ()


def _text(value: object, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise EdgarPayloadError(f"submissions field {field} must be text")
    return value.strip() or None


def _date(value: object, field: str, *, required: bool = True) -> date | None:
    text = _text(value, field, required=required)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise EdgarPayloadError(f"submissions field {field} is not an ISO date") from exc
        if parsed.tzinfo is None:
            raise EdgarPayloadError(
                f"submissions field {field} datetime must be timezone-aware"
            ) from exc
        return parsed.astimezone(UTC).date()


def _accepted(value: object) -> datetime:
    text = _text(value, "acceptanceDateTime")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EdgarPayloadError("submissions acceptanceDateTime is invalid") from exc
    if parsed.tzinfo is None:
        raise EdgarPayloadError("submissions acceptanceDateTime must be timezone-aware")
    return parsed.astimezone(UTC)


def _sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EdgarPayloadError(f"submissions field {field} must be an array")
    return value


def _filing_rows_with_gaps(
    payload: Mapping[str, object],
    *,
    cik: int,
    source_url: str,
    source_sha256: str,
) -> tuple[tuple[SubmissionFiling, ...], int]:
    columns = {name: _sequence(payload.get(name), name) for name in _REQUIRED_COLUMNS}
    row_count = len(columns["accessionNumber"])
    if any(len(values) != row_count for values in columns.values()):
        raise EdgarPayloadError("submissions filing arrays have different lengths")
    optional: dict[str, Sequence[object]] = {}
    for name in ("reportDate",):
        value = payload.get(name)
        if value is not None:
            values = _sequence(value, name)
            if len(values) != row_count:
                raise EdgarPayloadError("submissions optional filing array length mismatch")
            optional[name] = values

    rows: dict[str, SubmissionFiling] = {}
    unusable_filings = 0
    for index in range(row_count):
        accession = _text(columns["accessionNumber"][index], "accessionNumber")
        if not ACCESSION_PATTERN.fullmatch(accession):
            raise EdgarPayloadError(f"invalid SEC accession: {accession!r}")
        primary_raw = columns["primaryDocument"][index]
        if not isinstance(primary_raw, str) or not primary_raw.strip():
            unusable_filings += 1
            continue
        primary_document = primary_raw.strip()
        if ".." in primary_document or primary_document.startswith(("/", "\\")):
            unusable_filings += 1
            continue
        filing = SubmissionFiling(
            cik=cik,
            accession=accession,
            form=_text(columns["form"][index], "form"),
            filing_date=_date(columns["filingDate"][index], "filingDate"),
            accepted=_accepted(columns["acceptanceDateTime"][index]),
            report_date=(
                _date(optional["reportDate"][index], "reportDate", required=False)
                if "reportDate" in optional
                else None
            ),
            primary_document=primary_document,
            source_url=source_url,
            source_sha256=source_sha256,
        )
        prior = rows.get(accession)
        if prior is not None and prior != filing:
            raise EdgarPayloadError(f"conflicting submissions rows for {accession}")
        rows[accession] = filing
    return (
        tuple(sorted(rows.values(), key=lambda item: (item.accepted, item.accession))),
        unusable_filings,
    )


def _filing_rows(
    payload: Mapping[str, object],
    *,
    cik: int,
    source_url: str,
    source_sha256: str,
) -> tuple[SubmissionFiling, ...]:
    rows, _ = _filing_rows_with_gaps(
        payload,
        cik=cik,
        source_url=source_url,
        source_sha256=source_sha256,
    )
    return rows


def _form_rows(
    payload: Mapping[str, object],
    *,
    cik: int,
    source_url: str,
    source_sha256: str,
) -> tuple[SubmissionFormObservation, ...]:
    names = ("accessionNumber", "acceptanceDateTime", "form")
    columns = {name: _sequence(payload.get(name), name) for name in names}
    row_count = len(columns["accessionNumber"])
    if any(len(values) != row_count for values in columns.values()):
        raise EdgarPayloadError("submissions form-history arrays have different lengths")
    rows: dict[str, SubmissionFormObservation] = {}
    for index in range(row_count):
        accession = _text(columns["accessionNumber"][index], "accessionNumber")
        if not ACCESSION_PATTERN.fullmatch(accession):
            raise EdgarPayloadError(f"invalid SEC accession: {accession!r}")
        observation = SubmissionFormObservation(
            cik,
            accession,
            _text(columns["form"][index], "form"),
            _accepted(columns["acceptanceDateTime"][index]),
            source_url,
            source_sha256,
        )
        prior = rows.get(accession)
        if prior is not None and prior != observation:
            raise EdgarPayloadError(f"conflicting submissions form rows for {accession}")
        rows[accession] = observation
    return tuple(sorted(rows.values(), key=lambda item: (item.accepted, item.accession)))


def parse_submission_history(
    payload: Mapping[str, object],
    *,
    cik: int,
    source_url: str,
    source_sha256: str,
) -> tuple[SubmissionFiling, ...]:
    """Parse one paginated older submissions file, which is a columnar object."""
    return _filing_rows(
        payload,
        cik=cik,
        source_url=source_url,
        source_sha256=source_sha256,
    )


def parse_submission_history_forms(
    payload: Mapping[str, object],
    *,
    cik: int,
    source_url: str,
    source_sha256: str,
) -> tuple[SubmissionFormObservation, ...]:
    """Parse form metadata even when an older row has no archivable primary document."""
    return _form_rows(
        payload,
        cik=cik,
        source_url=source_url,
        source_sha256=source_sha256,
    )


def parse_submissions_document(document: EdgarDocument) -> SubmissionFeed:
    """Parse one current submissions document without inventing symbol history."""
    payload = document.payload
    raw_cik = payload.get("cik")
    try:
        cik = int(str(raw_cik))
    except (TypeError, ValueError) as exc:
        raise EdgarPayloadError("submissions cik is invalid") from exc
    if cik <= 0:
        raise EdgarPayloadError("submissions cik must be positive")
    filings_node = payload.get("filings")
    if not isinstance(filings_node, Mapping):
        raise EdgarPayloadError("submissions filings must be an object")
    recent = filings_node.get("recent")
    if not isinstance(recent, Mapping):
        raise EdgarPayloadError("submissions filings.recent must be an object")

    tickers = _sequence(payload.get("tickers", ()), "tickers")
    exchanges = _sequence(payload.get("exchanges", ()), "exchanges")
    if len(tickers) != len(exchanges):
        raise EdgarPayloadError("submissions current ticker/exchange arrays differ in length")
    current_symbols: list[CurrentSymbol] = []
    unusable_current_symbols = 0
    for ticker, exchange in zip(tickers, exchanges, strict=True):
        if (
            not isinstance(ticker, str)
            or not ticker.strip()
            or not isinstance(exchange, str)
            or not exchange.strip()
        ):
            unusable_current_symbols += 1
            continue
        current_symbols.append(CurrentSymbol(ticker.strip(), exchange.strip()))

    former_names: list[FormerName] = []
    for item in _sequence(payload.get("formerNames", ()), "formerNames"):
        if not isinstance(item, Mapping):
            raise EdgarPayloadError("submissions formerNames row must be an object")
        former_names.append(
            FormerName(
                _text(item.get("name"), "formerNames.name"),
                _date(item.get("from"), "formerNames.from", required=False),
                _date(item.get("to"), "formerNames.to", required=False),
            )
        )

    history_files: list[str] = []
    for item in _sequence(filings_node.get("files", ()), "filings.files"):
        if not isinstance(item, Mapping):
            raise EdgarPayloadError("submissions filings.files row must be an object")
        name = _text(item.get("name"), "filings.files.name")
        if not re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", name):
            raise EdgarPayloadError(f"unsafe submissions history filename: {name!r}")
        history_files.append(name)

    sic_value = payload.get("sic")
    try:
        sic = int(sic_value) if sic_value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise EdgarPayloadError("submissions sic is invalid") from exc
    recent_rows, unusable_filings = _filing_rows_with_gaps(
        recent,
        cik=cik,
        source_url=document.url,
        source_sha256=document.content_sha256,
    )
    form_history = _form_rows(
        recent,
        cik=cik,
        source_url=document.url,
        source_sha256=document.content_sha256,
    )
    return SubmissionFeed(
        cik=cik,
        name=_text(payload.get("name"), "name"),
        entity_type=_text(payload.get("entityType"), "entityType", required=False),
        sic=sic,
        state_of_incorporation=_text(
            payload.get("stateOfIncorporation"),
            "stateOfIncorporation",
            required=False,
        ),
        current_symbols=tuple(current_symbols),
        former_names=tuple(former_names),
        filings=recent_rows,
        history_files=tuple(history_files),
        observed_at=document.validated_at,
        source_url=document.url,
        source_sha256=document.content_sha256,
        unusable_filings=unusable_filings,
        unusable_current_symbols=unusable_current_symbols,
        form_history=form_history,
    )


def acceptance_by_accession(
    filings: Iterable[SubmissionFiling],
) -> dict[str, SubmissionFiling]:
    """Build an exact accession join and reject conflicting acceptance evidence."""
    result: dict[str, SubmissionFiling] = {}
    for filing in filings:
        prior = result.get(filing.accession)
        if prior is not None and prior != filing:
            raise EdgarPayloadError(f"conflicting filing evidence for {filing.accession}")
        result[filing.accession] = filing
    return result


def detect_new_periodic_filings(
    filings: Iterable[SubmissionFiling],
    *,
    seen_accessions: Iterable[str],
    as_of: datetime,
) -> tuple[SubmissionFiling, ...]:
    """Return only periodic filings accepted by the caller's PIT cutoff."""
    if as_of.tzinfo is None:
        raise EdgarPayloadError("periodic filing cutoff must be timezone-aware")
    seen = frozenset(seen_accessions)
    cutoff = as_of.astimezone(UTC)
    return tuple(
        sorted(
            (
                filing
                for filing in filings
                if filing.form in PERIODIC_FORMS
                and filing.accession not in seen
                and filing.accepted <= cutoff
            ),
            key=lambda item: (item.accepted, item.accession),
        )
    )
