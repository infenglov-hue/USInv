from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from usinv.data.edgar.client import EdgarDocument
from usinv.data.edgar.ticker_history_discovery import (
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
