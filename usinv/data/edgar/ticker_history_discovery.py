"""SEC full-text ticker discovery that never asserts security identity."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from usinv.data.edgar.client import EdgarClient, EdgarDocument, EdgarPayloadError
from usinv.data.edgar.name_discovery import (
    ExactNameMatch,
    normalize_company_name,
    normalize_company_stem,
)
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan

_CIK = re.compile(r"\(CIK\s+0*(\d{1,10})\)\s*$", re.IGNORECASE)
_DISPLAY_NAME = re.compile(
    r"\(([^()]*)\)\s+\(CIK\s+0*(\d{1,10})\)\s*$",
    re.IGNORECASE,
)


def parse_dominant_entity_candidate(
    document: EdgarDocument,
    *,
    minimum_hits: int = 5,
    dominance_ratio: int = 3,
) -> ExactNameMatch:
    """Return one dominant EFTS entity as a discovery candidate, if any."""

    try:
        buckets = document.payload["aggregations"]["entity_filter"]["buckets"]
    except (KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC full-text entity aggregation is invalid") from exc
    counts: dict[int, int] = defaultdict(int)
    for bucket in buckets:
        try:
            match = _CIK.search(bucket["key"])
            count = int(bucket["doc_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EdgarPayloadError("SEC full-text entity bucket is invalid") from exc
        if match is not None and count > 0:
            counts[int(match.group(1))] += count
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return ExactNameMatch("", "unmatched", (), ())
    top_cik, top_hits = ranked[0]
    second_hits = ranked[1][1] if len(ranked) > 1 else 0
    if top_hits < minimum_hits or (second_hits and top_hits < dominance_ratio * second_hits):
        return ExactNameMatch("", "unmatched", (), ())
    pointer = (
        f"{document.url}#entity-filter-cik-{top_cik}"
        f";sha256={document.content_sha256};hits={top_hits}"
    )
    return ExactNameMatch("", "unique", (top_cik,), (pointer,))


def parse_entity_candidates(
    document: EdgarDocument,
    *,
    maximum_candidates: int = 5,
) -> ExactNameMatch:
    """Retain bounded EFTS candidates for later exact cover verification.

    A dominant entity keeps the original strong-candidate semantics.  When the
    aggregation is not dominant, the bounded candidate set is deliberately
    marked weak: it may trigger cover acquisition, but it must never by itself
    support a filer-regime exclusion.
    """

    if maximum_candidates <= 0:
        raise EdgarPayloadError("historical ticker candidate limit is invalid")
    dominant = parse_dominant_entity_candidate(document)
    if dominant.status == "unique":
        return dominant
    try:
        buckets = document.payload["aggregations"]["entity_filter"]["buckets"]
    except (KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC full-text entity aggregation is invalid") from exc
    counts: dict[int, int] = defaultdict(int)
    for bucket in buckets:
        try:
            match = _CIK.search(bucket["key"])
            count = int(bucket["doc_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EdgarPayloadError("SEC full-text entity bucket is invalid") from exc
        if match is not None and count > 0:
            counts[int(match.group(1))] += count
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
        :maximum_candidates
    ]
    if not ranked:
        return ExactNameMatch("", "unmatched", (), ())
    ciks = tuple(sorted(cik for cik, _hits in ranked))
    pointers = tuple(
        sorted(
            f"{document.url}#entity-filter-cik-{cik}"
            f";sha256={document.content_sha256};hits={hits};confidence=weak"
            for cik, hits in ranked
        )
    )
    return ExactNameMatch(
        "",
        "unique" if len(ciks) == 1 else "ambiguous",
        ciks,
        pointers,
    )


def parse_filing_index_ticker_candidates(
    document: EdgarDocument,
    ticker: str,
) -> ExactNameMatch:
    """Find CIKs whose filing-index display name lists the exact ticker."""

    try:
        hits = document.payload["hits"]["hits"]
    except (KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC full-text filing hits are invalid") from exc
    if not isinstance(hits, list):
        raise EdgarPayloadError("SEC full-text filing hits are invalid")
    evidence: dict[int, set[str]] = defaultdict(set)
    wanted = ticker.upper()
    for hit in hits:
        try:
            source = hit["_source"]
            display_names = source["display_names"]
            accession = source["adsh"]
        except (KeyError, TypeError) as exc:
            raise EdgarPayloadError("SEC full-text filing hit is invalid") from exc
        if not isinstance(display_names, list) or not isinstance(accession, str):
            raise EdgarPayloadError("SEC full-text filing hit is invalid")
        for display_name in display_names:
            if not isinstance(display_name, str):
                raise EdgarPayloadError("SEC full-text display name is invalid")
            match = _DISPLAY_NAME.search(display_name)
            if match is None:
                continue
            listed_tickers = {
                value.strip().upper()
                for value in match.group(1).split(",")
                if value.strip()
            }
            if wanted not in listed_tickers:
                continue
            cik = int(match.group(2))
            evidence[cik].add(
                f"{document.url}#filing-index-{accession}-cik-{cik}"
                f";sha256={document.content_sha256};ticker={wanted}"
            )
    if not evidence:
        return ExactNameMatch("", "unmatched", (), ())
    ciks = tuple(sorted(evidence))
    pointers = tuple(sorted(pointer for values in evidence.values() for pointer in values))
    return ExactNameMatch(
        "",
        "unique" if len(ciks) == 1 else "ambiguous",
        ciks,
        pointers,
    )


def parse_exact_entity_name_candidate(
    document: EdgarDocument,
    listing_name: str,
) -> ExactNameMatch:
    """Match an Alpha issuer name to exact normalized EFTS entity buckets."""
    wanted = normalize_company_name(listing_name)
    if not wanted:
        return ExactNameMatch("", "blank", (), ())
    try:
        buckets = document.payload["aggregations"]["entity_filter"]["buckets"]
    except (KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC full-text entity aggregation is invalid") from exc
    evidence: dict[int, set[str]] = defaultdict(set)
    for bucket in buckets:
        try:
            key = bucket["key"]
            hits = int(bucket["doc_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EdgarPayloadError("SEC full-text entity bucket is invalid") from exc
        if not isinstance(key, str) or hits <= 0:
            continue
        cik_match = _CIK.search(key)
        if cik_match is None:
            continue
        entity_name = key[: cik_match.start()].strip()
        ticker_suffix = re.search(r"\s+\([A-Z0-9.,/ -]+\)\s*$", entity_name)
        if ticker_suffix is not None:
            entity_name = entity_name[: ticker_suffix.start()].strip()
        if normalize_company_name(entity_name) != wanted:
            continue
        cik = int(cik_match.group(1))
        evidence[cik].add(
            f"{document.url}#exact-entity-name-cik-{cik}"
            f";sha256={document.content_sha256};hits={hits}"
        )
    ciks = tuple(sorted(evidence))
    pointers = tuple(sorted(pointer for values in evidence.values() for pointer in values))
    if not ciks:
        return ExactNameMatch(wanted, "unmatched", (), ())
    return ExactNameMatch(
        wanted,
        "unique" if len(ciks) == 1 else "ambiguous",
        ciks,
        pointers,
    )


def parse_unique_entity_stem_candidates(
    document: EdgarDocument,
    listing_name: str,
) -> ExactNameMatch:
    """Return suffix-insensitive EFTS candidates as weak cover targets only."""
    wanted = normalize_company_stem(listing_name)
    if not wanted:
        return ExactNameMatch("", "blank", (), ())
    try:
        buckets = document.payload["aggregations"]["entity_filter"]["buckets"]
    except (KeyError, TypeError) as exc:
        raise EdgarPayloadError("SEC full-text entity aggregation is invalid") from exc
    evidence: dict[int, set[str]] = defaultdict(set)
    for bucket in buckets:
        try:
            key = bucket["key"]
            hits = int(bucket["doc_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise EdgarPayloadError("SEC full-text entity bucket is invalid") from exc
        if not isinstance(key, str) or hits <= 0:
            continue
        cik_match = _CIK.search(key)
        if cik_match is None:
            continue
        entity_name = key[: cik_match.start()].strip()
        ticker_suffix = re.search(r"\s+\([A-Z0-9.,/ -]+\)\s*$", entity_name)
        if ticker_suffix is not None:
            entity_name = entity_name[: ticker_suffix.start()].strip()
        if normalize_company_stem(entity_name) != wanted:
            continue
        cik = int(cik_match.group(1))
        evidence[cik].add(
            f"{document.url}#entity-stem-cik-{cik}"
            f";sha256={document.content_sha256};hits={hits};confidence=weak"
        )
    ciks = tuple(sorted(evidence))
    pointers = tuple(sorted(pointer for values in evidence.values() for pointer in values))
    if not ciks:
        return ExactNameMatch(wanted, "unmatched", (), ())
    return ExactNameMatch(
        wanted,
        "unique" if len(ciks) == 1 else "ambiguous",
        ciks,
        pointers,
    )


def discover_historical_ticker_candidates(
    client: EdgarClient,
    discovery: FilingDiscoveryPlan,
    *,
    as_of: datetime,
    start: date = date(2009, 1, 1),
    parallel_queries: int = 4,
    target_tickers: frozenset[str] | None = None,
    listing_names_by_pointer: Mapping[str, str] | None = None,
    refresh: bool = False,
) -> dict[str, ExactNameMatch]:
    """Query each unresolved ticker and retain only dominant SEC entity candidates."""

    if as_of.tzinfo is None or parallel_queries <= 0 or start > as_of.date():
        raise EdgarPayloadError("historical ticker discovery bounds are invalid")
    all_rows = tuple(row for row in discovery.rows if row.status == "unmapped")
    rows = tuple(
        row
        for row in all_rows
        if target_tickers is None or row.ticker in target_tickers
    )

    def discover(row) -> ExactNameMatch:
        document = client.full_text_search(
            row.ticker,
            start=start,
            end=as_of.date(),
            refresh=refresh,
        )
        indexed = parse_filing_index_ticker_candidates(document, row.ticker)
        if indexed.status != "unmatched":
            return indexed
        listing_name = (
            listing_names_by_pointer.get(row.listing_evidence_pointer, "")
            if listing_names_by_pointer is not None
            else ""
        )
        named = parse_exact_entity_name_candidate(document, listing_name)
        if named.status not in {"blank", "unmatched"}:
            return named
        stemmed = parse_unique_entity_stem_candidates(document, listing_name)
        return (
            stemmed
            if stemmed.status not in {"blank", "unmatched"}
            else parse_dominant_entity_candidate(document)
        )

    with ThreadPoolExecutor(max_workers=min(parallel_queries, max(1, len(rows)))) as executor:
        matches = tuple(executor.map(discover, rows))
    matched = {
        row.listing_evidence_pointer: match
        for row, match in zip(rows, matches, strict=True)
    }
    unmatched = ExactNameMatch("", "unmatched", (), ())
    return {
        row.listing_evidence_pointer: matched.get(row.listing_evidence_pointer, unmatched)
        for row in all_rows
    }
