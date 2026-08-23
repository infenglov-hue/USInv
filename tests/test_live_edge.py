from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from usinv.data.edgar.client import EdgarDocument, EdgarPayloadError
from usinv.data.edgar.companyfacts import (
    compare_edge_to_fsds,
    parse_companyfacts_document,
)
from usinv.data.edgar.live_edge import read_live_edge_snapshot
from usinv.data.edgar.periods import fsds_period, nearest_month_end, quarter_count
from usinv.data.edgar.submissions import (
    SubmissionFiling,
    detect_new_periodic_filings,
    parse_submission_history,
    parse_submission_history_forms,
    parse_submissions_document,
)
from usinv.data.edgar.tag_chains import RawFact

CIK = 320193
ACCN = "0000320193-25-000001"
URL = "https://data.sec.gov/submissions/CIK0000320193.json"
SHA = "a" * 64
PARITY_FIXTURE = Path(__file__).parent / "fixtures" / "edgar" / "apple_2025_10k_parity.json"


def _document(payload: dict[str, object], url: str = URL) -> EdgarDocument:
    observed = datetime(2026, 7, 19, 12, tzinfo=UTC)
    return EdgarDocument(url, observed, observed, SHA, payload, False, False)


def test_live_edge_snapshot_reader_restores_verified_pit_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_id = "b" * 64
    root = tmp_path / snapshot_id
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "live_edge_version": "usinv-live-edge-v2",
                "snapshot_id": snapshot_id,
                "batch_id": "sec-live:accession:hash",
                "source_quarter": "2026q2",
                "source_sha256": "c" * 64,
                "cik": CIK,
                "accession": ACCN,
                "accepted": "2026-05-02T20:00:00+00:00",
                "filing_sic": 3571,
                "filing_sic_evidence_pointer": "sec-archive://filing.txt#sic",
                "artifacts": {
                    "facts_raw": {"rows": 12},
                    "filing_facts": {"rows": 14},
                    "presentation": {"rows": 3},
                },
                "issues": [
                    {
                        "kind": "fixture",
                        "tag": None,
                        "context_id": None,
                        "detail": "retained issue",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("usinv.data.edgar.live_edge._verify", lambda *_: None)

    snapshot = read_live_edge_snapshot(root)

    assert snapshot.batch_id == "sec-live:accession:hash"
    assert snapshot.source_quarter == "2026q2"
    assert snapshot.facts_raw_rows == 12
    assert snapshot.filing_sic == 3571
    assert snapshot.issues[0].detail == "retained issue"


def _submissions_payload() -> dict[str, object]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "entityType": "operating",
        "sic": "3571",
        "stateOfIncorporation": "CA",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "formerNames": [
            {
                "name": "Apple Computer, Inc.",
                "from": "1977-01-03",
                "to": "2007-01-10T05:00:00.000Z",
            }
        ],
        "filings": {
            "recent": {
                "accessionNumber": [ACCN, "0000320193-26-000002"],
                "filingDate": ["2025-05-02", "2026-08-01"],
                "acceptanceDateTime": [
                    "2025-05-02T20:00:00.000Z",
                    "2026-08-01T20:00:00.000Z",
                ],
                "form": ["10-Q", "10-Q"],
                "reportDate": ["2025-03-31", "2026-06-30"],
                "primaryDocument": ["aapl-20250331.htm", "aapl-20260630.htm"],
            },
            "files": [{"name": "CIK0000320193-submissions-001.json", "filingCount": 1000}],
        },
    }


def test_submissions_parser_keeps_current_state_separate_from_filing_history() -> None:
    feed = parse_submissions_document(_document(_submissions_payload()))

    assert feed.cik == CIK
    assert feed.current_symbols[0].ticker == "AAPL"
    assert feed.former_names[0].name == "Apple Computer, Inc."
    assert feed.former_names[0].valid_to == date(2007, 1, 10)
    assert feed.filings[0].accepted == datetime(2025, 5, 2, 20, tzinfo=UTC)
    assert len(feed.form_history) == 2
    assert feed.history_files == ("CIK0000320193-submissions-001.json",)
    assert feed.unusable_filings == 0
    assert feed.unusable_current_symbols == 0


def test_submissions_parser_quarantines_incomplete_current_symbol_rows() -> None:
    payload = _submissions_payload()
    payload["tickers"] = ["AAPL", "BROKEN"]
    payload["exchanges"] = ["Nasdaq", None]

    feed = parse_submissions_document(_document(payload))

    assert [(row.ticker, row.exchange) for row in feed.current_symbols] == [("AAPL", "Nasdaq")]
    assert feed.unusable_current_symbols == 1


def test_submissions_parser_quarantines_rows_without_a_primary_document() -> None:
    payload = _submissions_payload()
    payload["filings"]["recent"]["primaryDocument"][0] = ""  # type: ignore[index]

    feed = parse_submissions_document(_document(payload))

    assert feed.unusable_filings == 1
    assert len(feed.filings) == 1


def test_submission_arrays_and_history_filenames_fail_closed() -> None:
    payload = _submissions_payload()
    payload["tickers"] = ["AAPL", "EXTRA"]
    with pytest.raises(EdgarPayloadError, match="differ in length"):
        parse_submissions_document(_document(payload))

    payload = _submissions_payload()
    payload["filings"]["files"][0]["name"] = "../escape.json"
    with pytest.raises(EdgarPayloadError, match="unsafe"):
        parse_submissions_document(_document(payload))

    payload = _submissions_payload()
    payload["filings"]["recent"]["primaryDocument"][0] = "../escape.htm"  # type: ignore[index]
    feed = parse_submissions_document(_document(payload))
    assert feed.unusable_filings == 1 and len(feed.filings) == 1
    assert len(feed.form_history) == 2


def test_periodic_detection_cannot_see_a_future_acceptance() -> None:
    feed = parse_submissions_document(_document(_submissions_payload()))

    selected = detect_new_periodic_filings(
        feed.filings,
        seen_accessions=(),
        as_of=datetime(2026, 7, 19, 16, tzinfo=UTC),
    )

    assert [item.accession for item in selected] == [ACCN]
    with pytest.raises(EdgarPayloadError, match="timezone-aware"):
        detect_new_periodic_filings(
            feed.filings,
            seen_accessions=(),
            as_of=datetime(2026, 7, 19),
        )


def test_periodic_detection_can_select_only_the_unpublished_fsds_window() -> None:
    feed = parse_submissions_document(_document(_submissions_payload()))

    selected = detect_new_periodic_filings(
        feed.filings,
        seen_accessions=(),
        accepted_after=datetime(2025, 3, 31, 23, 59, 59, tzinfo=UTC),
        as_of=datetime(2025, 7, 17, 20, tzinfo=UTC),
    )

    assert [item.accession for item in selected] == [ACCN]
    with pytest.raises(EdgarPayloadError, match="precede"):
        detect_new_periodic_filings(
            feed.filings,
            seen_accessions=(),
            accepted_after=datetime(2025, 7, 17, 20, tzinfo=UTC),
            as_of=datetime(2025, 7, 17, 20, tzinfo=UTC),
        )


def test_older_submission_page_uses_same_acceptance_contract() -> None:
    recent = _submissions_payload()["filings"]["recent"]
    filings = parse_submission_history(
        recent,
        cik=CIK,
        source_url="https://data.sec.gov/submissions/history.json",
        source_sha256=SHA,
    )

    assert len(filings) == 2
    assert all(item.accepted.tzinfo is UTC for item in filings)

    recent["primaryDocument"][0] = None
    forms = parse_submission_history_forms(
        recent,
        cik=CIK,
        source_url="https://data.sec.gov/submissions/history.json",
        source_sha256=SHA,
    )
    assert len(forms) == 2 and forms[0].accession == ACCN


def test_period_normalization_ignores_filing_fy_fp() -> None:
    assert nearest_month_end(date(2026, 4, 17)) == date(2026, 4, 30)
    assert nearest_month_end(date(2026, 4, 1)) == date(2026, 3, 31)
    assert quarter_count(date(2025, 1, 1), date(2025, 6, 30)) == 2
    assert fsds_period(date(2025, 4, 1), date(2025, 6, 28)) == (date(2025, 6, 30), 1)
    with pytest.raises(EdgarPayloadError, match="does not map"):
        quarter_count(date(2025, 1, 1), date(2026, 12, 31))


def _filing() -> SubmissionFiling:
    return SubmissionFiling(
        cik=CIK,
        accession=ACCN,
        form="10-Q",
        filing_date=date(2025, 5, 2),
        accepted=datetime(2025, 5, 2, 20, tzinfo=UTC),
        report_date=date(2025, 3, 31),
        primary_document="aapl-20250331.htm",
        source_url=URL,
        source_sha256=SHA,
    )


def test_companyfacts_joins_acceptance_and_derives_period_without_fy_fp() -> None:
    payload = {
        "cik": CIK,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-03-29",
                                "val": 100,
                                "accn": ACCN,
                                "fy": 2099,
                                "fp": "FY",
                                "form": "10-Q",
                                "filed": "1900-01-01",
                            }
                        ]
                    }
                }
            }
        },
    }

    result = parse_companyfacts_document(_document(payload), filings=[_filing()])

    assert not result.issues
    assert result.facts[0].ddate == date(2025, 3, 31)
    assert result.facts[0].qtrs == 1
    assert result.facts[0].accepted == _filing().accepted
    assert result.facts[0].filed == _filing().filing_date


def test_companyfacts_never_uses_filed_date_when_acceptance_join_is_missing() -> None:
    payload = {
        "cik": CIK,
        "entityName": "Apple Inc.",
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2025-04-17",
                                "val": 10,
                                "accn": "0000320193-25-999999",
                                "form": "10-Q",
                                "filed": "2025-05-02",
                            }
                        ]
                    }
                }
            }
        },
    }

    result = parse_companyfacts_document(_document(payload), filings=[_filing()])

    assert not result.facts
    assert result.issues[0].kind == "missing_acceptance_join"


def test_companyfacts_conflict_stays_quarantined_after_later_duplicate() -> None:
    rows = [
        {
            "start": "2025-01-01",
            "end": "2025-03-29",
            "val": value,
            "accn": ACCN,
            "form": "10-Q",
        }
        for value in (100, 101, 100)
    ]
    payload = {
        "cik": CIK,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": rows}},
            }
        },
    }

    result = parse_companyfacts_document(_document(payload), filings=[_filing()])

    assert not result.facts
    assert [issue.kind for issue in result.issues] == ["same_accession_conflict"]


def test_retrospective_edge_fsds_parity_names_each_mismatch() -> None:
    edge = RawFact(
        cik=CIK,
        tag="Revenues",
        ddate=date(2025, 3, 31),
        qtrs=1,
        uom="USD",
        value=Decimal("100"),
        accepted=datetime(2025, 5, 2, 20, tzinfo=UTC),
        adsh=ACCN,
        version="us-gaap",
        form="10-Q",
        filed=date(2025, 5, 2),
        filing_period=date(2025, 3, 31),
    )
    match = compare_edge_to_fsds([edge], [edge])
    changed = RawFact(
        cik=edge.cik,
        tag=edge.tag,
        ddate=edge.ddate,
        qtrs=edge.qtrs,
        uom=edge.uom,
        value=Decimal("101"),
        accepted=edge.accepted,
        adsh=edge.adsh,
        version=edge.version,
        form=edge.form,
        filed=edge.filed,
        filing_period=edge.filing_period,
    )

    mismatch = compare_edge_to_fsds([changed], [edge])

    assert match.passed and match.matched_keys == 1
    assert not mismatch.passed
    assert mismatch.mismatches[0].reason == "value_mismatch"


def test_official_apple_2025_10k_companyfacts_matches_fsds_golden() -> None:
    fixture = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
    provenance = fixture["_fixture"]
    observed = datetime.fromisoformat(provenance["retrieved_at"])
    submissions_document = EdgarDocument(
        provenance["submissions_url"],
        observed,
        observed,
        provenance["submissions_sha256"],
        fixture["submissions"],
        False,
        False,
    )
    companyfacts_document = EdgarDocument(
        provenance["companyfacts_url"],
        observed,
        observed,
        provenance["companyfacts_sha256"],
        fixture["companyfacts"],
        False,
        False,
    )
    feed = parse_submissions_document(submissions_document)
    edge = parse_companyfacts_document(companyfacts_document, filings=feed.filings)
    filing = feed.filings[0]
    fsds = [
        RawFact(
            cik=feed.cik,
            tag=row["tag"],
            ddate=date.fromisoformat(row["ddate"]),
            qtrs=row["qtrs"],
            uom=row["uom"],
            value=Decimal(row["value"]),
            accepted=filing.accepted,
            adsh=filing.accession,
            version="us-gaap/2025",
            form=filing.form,
            filed=filing.filing_date,
            filing_period=filing.report_date,
        )
        for row in fixture["fsds_expected"]
    ]

    report = compare_edge_to_fsds(edge.facts, fsds)

    assert provenance["fsds_quarter"] == "2025q4"
    assert provenance["fsds_sha256"] == (
        "2b36ac3850c022cf19edd882e31c3c453c7666677b9fdd2e1f7748fdb5768c6e"
    )
    assert report.passed
    assert report.compared_keys == report.matched_keys == 11
