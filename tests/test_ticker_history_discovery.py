from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

from usinv.data.edgar.client import EdgarDocument
from usinv.data.edgar.security_bootstrap import FilingDiscoveryPlan, FilingDiscoveryRow
from usinv.data.edgar.ticker_history_discovery import (
    augment_discovery_plan_with_ticker_evidence,
    corroborate_historical_association_candidates,
    corroborate_historical_ticker_candidates,
    discover_historical_ticker_candidates,
    parse_association_entity_stem_candidate,
    parse_dominant_entity_candidate,
    parse_entity_candidates,
    parse_exact_entity_name_candidate,
    parse_filing_index_ticker_candidates,
    parse_unique_entity_stem_candidates,
)


def _document(buckets: list[dict[str, object]]) -> EdgarDocument:
    payload = {
        "hits": {"total": {"value": sum(int(row["doc_count"]) for row in buckets)}},
        "aggregations": {"entity_filter": {"buckets": buckets}},
    }
    body = json.dumps(payload, sort_keys=True).encode()
    observed = datetime(2026, 7, 17, 21, tzinfo=UTC)
    return EdgarDocument(
        "https://efts.sec.gov/LATEST/search-index?q=UCBI",
        observed,
        observed,
        hashlib.sha256(body).hexdigest(),
        payload,
        False,
        False,
    )


def _document_with_hits(
    display_names: list[str],
    *,
    query: str = "WFC",
) -> EdgarDocument:
    payload = {
        "hits": {
            "total": {"value": len(display_names)},
            "hits": [
                {
                    "_source": {
                        "display_names": [display_name],
                        "adsh": f"0000000001-25-{index:06d}",
                    }
                }
                for index, display_name in enumerate(display_names, start=1)
            ],
        },
        "aggregations": {"entity_filter": {"buckets": []}},
    }
    body = json.dumps(payload, sort_keys=True).encode()
    observed = datetime(2026, 7, 17, 21, tzinfo=UTC)
    return EdgarDocument(
        f"https://efts.sec.gov/LATEST/search-index?q={query}",
        observed,
        observed,
        hashlib.sha256(body).hexdigest(),
        payload,
        False,
        False,
    )


def test_dominant_full_text_entity_is_only_a_provenanced_candidate() -> None:
    match = parse_dominant_entity_candidate(
        _document(
            [
                {
                    "key": "UNITED COMMUNITY BANKS INC (CIK 0000857855)",
                    "doc_count": 475,
                },
                {"key": "OTHER CORP (CIK 0000000002)", "doc_count": 34},
            ]
        )
    )

    assert match.status == "unique"
    assert match.candidate_ciks == (857855,)
    assert "sha256=" in match.evidence_pointers[0]
    assert "confidence=weak" in match.evidence_pointers[0]


def test_non_dominant_full_text_entities_remain_unmatched() -> None:
    match = parse_dominant_entity_candidate(
        _document(
            [
                {"key": "FIRST CORP (CIK 0000000001)", "doc_count": 8},
                {"key": "SECOND CORP (CIK 0000000002)", "doc_count": 7},
            ]
        )
    )

    assert match.status == "unmatched"
    assert not match.candidate_ciks


def test_non_dominant_entities_are_bounded_weak_cover_candidates() -> None:
    match = parse_entity_candidates(
        _document(
            [
                {"key": "FIRST CORP (CIK 0000000001)", "doc_count": 8},
                {"key": "SECOND CORP (CIK 0000000002)", "doc_count": 7},
                {"key": "THIRD CORP (CIK 0000000003)", "doc_count": 6},
            ]
        ),
        maximum_candidates=2,
    )

    assert match.status == "ambiguous"
    assert match.candidate_ciks == (1, 2)
    assert all("confidence=weak" in pointer for pointer in match.evidence_pointers)


def test_filing_index_requires_exact_display_name_ticker() -> None:
    match = parse_filing_index_ticker_candidates(
        _document_with_hits(
            [
                "WELLS FARGO & COMPANY (WFC, WFC-PY) (CIK 0000072971)",
                "OTHER CORP (WFCA) (CIK 0000000002)",
            ]
        ),
        "WFC",
    )

    assert match.status == "unique"
    assert match.candidate_ciks == (72971,)
    assert "ticker=WFC" in match.evidence_pointers[0]


def test_exact_efts_entity_name_recovers_non_dominant_candidate() -> None:
    match = parse_exact_entity_name_candidate(
        _document(
            [
                {"key": "OTHER CORP (OTHR) (CIK 0000000002)", "doc_count": 500},
                {
                    "key": "CRAFT BREW ALLIANCE, INC. (CIK 0000892222)",
                    "doc_count": 20,
                },
            ]
        ),
        "Craft Brew Alliance Inc",
    )

    assert match.status == "unique"
    assert match.candidate_ciks == (892222,)
    assert "exact-entity-name" in match.evidence_pointers[0]


def test_exact_efts_entity_name_keeps_duplicate_names_ambiguous() -> None:
    match = parse_exact_entity_name_candidate(
        _document(
            [
                {"key": "SAME CORP (ONE) (CIK 0000000001)", "doc_count": 8},
                {"key": "Same Corp (TWO) (CIK 0000000002)", "doc_count": 7},
            ]
        ),
        "Same Corp",
    )

    assert match.status == "ambiguous"
    assert match.candidate_ciks == (1, 2)


def test_entity_stem_candidate_is_explicitly_weak() -> None:
    match = parse_unique_entity_stem_candidates(
        _document(
            [
                {
                    "key": "CHANNEL THERAPEUTICS CORP (CIK 0001919246)",
                    "doc_count": 12,
                },
            ]
        ),
        "Channel Therapeutics Corporation",
    )

    assert match.status == "unique"
    assert match.candidate_ciks == (1919246,)
    assert "confidence=weak" in match.evidence_pointers[0]


def test_unique_nontrivial_stem_corroborates_existing_candidate() -> None:
    match = parse_association_entity_stem_candidate(
        _document(
            [
                {
                    "key": "COLLECTIVE ACQUISITION CORP (CIK 0002041047)",
                    "doc_count": 12,
                },
            ]
        ),
        "Collective Acquisition Corp - Class A",
        2041047,
    )

    assert match.status == "unique"
    assert match.candidate_ciks == (2041047,)
    assert "confidence=weak" not in match.evidence_pointers[0]
    assert (
        parse_association_entity_stem_candidate(
            _document([{"key": "DPC HOLDINGS LTD (CIK 0002107018)", "doc_count": 5}]),
            "DPC Holdings PLC",
            2107018,
        ).status
        == "unmatched"
    )


def test_association_corroboration_requires_the_same_unique_cik() -> None:
    document = _document(
        [{"key": "COLLECTIVE ACQUISITION CORP (CIK 0002041047)", "doc_count": 12}]
    )

    class Client:
        def full_text_search(
            self,
            query: str,
            *,
            start: date,
            end: date,
            forms: tuple[str, ...],
            refresh: bool,
        ) -> EdgarDocument:
            assert query.startswith("COLLECTIVE ACQUISITION")
            return document

    pointer = "alpha-vantage://listing/association"
    discovery = SimpleNamespace(
        rows=(
            SimpleNamespace(
                status="discovered",
                ticker="CCAQ",
                candidate_ciks=(2041047,),
                candidate_evidence_pointers=(),
                listing_evidence_pointer=pointer,
            ),
        )
    )

    matches = corroborate_historical_association_candidates(
        Client(),  # type: ignore[arg-type]
        discovery,  # type: ignore[arg-type]
        as_of=datetime(2026, 7, 17, 20, tzinfo=UTC),
        listing_names_by_pointer={pointer: "Collective Acquisition Corp - Class A"},
        parallel_queries=1,
    )

    assert matches[pointer].candidate_ciks == (2041047,)
    assert "confidence=weak" not in matches[pointer].evidence_pointers[0]


def test_exact_ticker_corroboration_merges_provenance_without_changing_cik() -> None:
    document = _document_with_hits(
        ["ISSUER INC (ONE) (CIK 0000000123)"],
        query="ONE",
    )

    class Client:
        def full_text_search(
            self,
            query: str,
            *,
            start: date,
            end: date,
            forms: tuple[str, ...],
            refresh: bool,
        ) -> EdgarDocument:
            assert query == "ONE"
            assert start == date(2001, 1, 1)
            assert end == date(2026, 7, 17)
            assert "10-Q" in forms and not refresh
            return document

    pointer = "alpha-vantage://listing/1"
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "a" * 64,
        "b" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ONE",
                "NASDAQ",
                "NASDAQ",
                "Stock",
                pointer,
                "discovered",
                (123,),
                ("sec-fsds://source/accession?cik=123#issuer-name",),
            ),
        ),
    )

    matches = corroborate_historical_ticker_candidates(
        Client(),  # type: ignore[arg-type]
        discovery,
        as_of=datetime(2026, 7, 17, 20, tzinfo=UTC),
        parallel_queries=1,
    )
    augmented = augment_discovery_plan_with_ticker_evidence(discovery, matches)

    assert augmented.rows[0].candidate_ciks == (123,)
    assert augmented.rows[0].candidate_evidence_pointers[0].startswith("https://efts.sec.gov")
    assert augmented.rows[0].candidate_evidence_pointers[1].startswith("sec-fsds://")


def test_ticker_corroboration_rejects_a_different_or_ambiguous_cik() -> None:
    document = _document_with_hits(
        [
            "WRONG INC (ONE) (CIK 0000000999)",
            "OTHER INC (ONE) (CIK 0000000888)",
        ],
        query="ONE",
    )

    class Client:
        def full_text_search(self, *args: object, **kwargs: object) -> EdgarDocument:
            return document

    pointer = "alpha-vantage://listing/1"
    discovery = FilingDiscoveryPlan(
        date(2026, 7, 17),
        "a" * 64,
        "b" * 64,
        datetime(2026, 7, 17, 21, tzinfo=UTC),
        (
            FilingDiscoveryRow(
                "ONE",
                "NASDAQ",
                "NASDAQ",
                "Stock",
                pointer,
                "discovered",
                (123,),
            ),
        ),
    )

    matches = corroborate_historical_ticker_candidates(
        Client(),  # type: ignore[arg-type]
        discovery,
        as_of=datetime(2026, 7, 17, 20, tzinfo=UTC),
        parallel_queries=1,
    )
    augmented = augment_discovery_plan_with_ticker_evidence(discovery, matches)

    assert matches[pointer].status == "unmatched"
    assert not augmented.rows[0].candidate_evidence_pointers


def test_discovery_queries_listing_name_when_ticker_result_has_no_entity_match() -> None:
    ticker_document = _document_with_hits([], query="OLD")
    name_document = _document(
        [{"key": "OLD COMPANY INC (CIK 0000123456)", "doc_count": 12}]
    )

    class Client:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def full_text_search(
            self,
            query: str,
            *,
            start: date,
            end: date,
            forms: tuple[str, ...],
            refresh: bool,
        ) -> EdgarDocument:
            self.queries.append(query)
            assert "S-1" in forms
            assert "10-KSB" in forms
            assert "SB-2" in forms
            assert "N-2" in forms
            assert "15-12B" in forms
            return name_document if query == "OLD COMPANY INC" else ticker_document

    pointer = "alpha-vantage://listing/1"
    discovery = SimpleNamespace(
        rows=(
            SimpleNamespace(
                status="unmapped",
                ticker="OLD",
                listing_evidence_pointer=pointer,
            ),
        )
    )
    client = Client()

    matches = discover_historical_ticker_candidates(
        client,
        discovery,
        as_of=datetime(2026, 7, 17, 20, tzinfo=UTC),
        parallel_queries=1,
        listing_names_by_pointer={pointer: "Old Company Inc"},
    )

    assert client.queries == ["OLD", "OLD COMPANY INC"]
    assert matches[pointer].candidate_ciks == (123456,)


def test_discovery_falls_back_to_auxiliary_proxy_forms() -> None:
    empty = _document_with_hits([], query="OLD")
    proxy = _document_with_hits(
        ["OLD COMPANY INC (OLD) (CIK 0000123456)"],
        query="OLD",
    )

    class Client:
        def __init__(self) -> None:
            self.form_sets: list[tuple[str, ...]] = []

        def full_text_search(
            self,
            query: str,
            *,
            start: date,
            end: date,
            forms: tuple[str, ...],
            refresh: bool,
        ) -> EdgarDocument:
            self.form_sets.append(forms)
            return proxy if "DEF 14A" in forms and query == "OLD" else empty

    pointer = "alpha-vantage://listing/1"
    discovery = SimpleNamespace(
        rows=(
            SimpleNamespace(
                status="unmapped",
                ticker="OLD",
                listing_evidence_pointer=pointer,
            ),
        )
    )
    client = Client()

    matches = discover_historical_ticker_candidates(
        client,  # type: ignore[arg-type]
        discovery,  # type: ignore[arg-type]
        as_of=datetime(2026, 7, 17, 20, tzinfo=UTC),
        parallel_queries=1,
        listing_names_by_pointer={pointer: ""},
    )

    assert any("DEF 14A" in forms for forms in client.form_sets)
    assert matches[pointer].candidate_ciks == (123456,)
