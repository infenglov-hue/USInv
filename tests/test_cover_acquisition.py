from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from usinv.data.edgar.client import EdgarDocument, EdgarHttpError, EdgarResource
from usinv.data.edgar.cover_acquisition import acquire_cover_evidence, infer_domestic_flag
from usinv.data.edgar.security_bootstrap import (
    FilingDiscoveryPlan,
    FilingDiscoveryRow,
    build_cover_security_master,
)
from usinv.data.edgar.submissions import SubmissionFiling

OBSERVED = datetime(2026, 7, 20, 12, tzinfo=UTC)
CUTOFF = datetime(2026, 7, 17, 20, tzinfo=UTC)
ACCESSION = "0000000001-26-000001"
PRIMARY = "issuer-20260331.htm"


def _document(payload: dict[str, object], url: str) -> EdgarDocument:
    body = json.dumps(payload, sort_keys=True).encode()
    return EdgarDocument(
        url,
        OBSERVED,
        OBSERVED,
        hashlib.sha256(body).hexdigest(),
        payload,
        False,
        False,
    )


def _filing_payload() -> dict[str, object]:
    return {
        "cik": "1",
        "name": "Issuer One",
        "entityType": "operating",
        "sic": "3571",
        "stateOfIncorporation": "DE",
        "tickers": ["ONE"],
        "exchanges": ["Nasdaq"],
        "formerNames": [],
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION, "0000000001-26-000002"],
                "filingDate": ["2026-05-01", "2026-07-18"],
                "acceptanceDateTime": ["2026-05-01T20:00:00Z", "2026-07-18T20:00:00Z"],
                "form": ["10-Q", "10-Q"],
                "primaryDocument": [PRIMARY, "future.htm"],
                "reportDate": ["2026-03-31", "2026-06-30"],
            },
            "files": [],
        },
    }


def _inline_xbrl(ticker: str = "ONE") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:dei="http://xbrl.sec.gov/dei/2025">
 <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
 <xbrli:context id="cover">
  <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
 </xbrli:context>
 <dei:TradingSymbol contextRef="cover">{ticker}</dei:TradingSymbol>
 <dei:SecurityExchangeName contextRef="cover">NASDAQ</dei:SecurityExchangeName>
 <dei:Security12bTitle contextRef="cover">Common Stock</dei:Security12bTitle>
 <dei:EntityCommonStockSharesOutstanding contextRef="cover" unitRef="shares" decimals="0">
  125000000
 </dei:EntityCommonStockSharesOutstanding>
</xbrli:xbrl>
""".encode()


class FakeClient:
    def __init__(self, ticker: str = "ONE") -> None:
        self.primary = _inline_xbrl(ticker)
        self.calls: list[str] = []

    def submissions(self, cik: int, *, refresh: bool) -> EdgarDocument:
        assert cik == 1 and not refresh
        return _document(
            _filing_payload(),
            "https://data.sec.gov/submissions/CIK0000000001.json",
        )

    def filing_resource(
        self,
        cik: int,
        accession: str,
        filename: str,
        *,
        refresh: bool = False,
    ) -> EdgarResource:
        assert cik == 1 and accession == ACCESSION and not refresh
        self.calls.append(filename)
        if filename == "index.json":
            body = json.dumps({"directory": {"item": [{"name": PRIMARY}]}}).encode()
        else:
            assert filename == PRIMARY
            body = self.primary
        return EdgarResource(
            f"https://www.sec.gov/Archives/edgar/data/1/{accession}/{filename}",
            OBSERVED,
            OBSERVED,
            hashlib.sha256(body).hexdigest(),
            body,
            False,
            False,
        )


def _plan() -> FilingDiscoveryPlan:
    rows = (
        FilingDiscoveryRow(
            "ONE",
            "NASDAQ",
            "NASDAQ",
            "Stock",
            "alpha-vantage://one/1",
            "discovered",
            (1,),
        ),
        FilingDiscoveryRow(
            "TWO",
            "NYSE",
            "NYSE",
            "Stock",
            "alpha-vantage://two/2",
            "discovered",
            (2,),
        ),
    )
    return FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, rows)


def test_cover_acquisition_is_pit_bounded_shardable_and_filing_backed(tmp_path: Path) -> None:
    result = acquire_cover_evidence(
        FakeClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert result.requested_ciks == (1,) and result.deferred_ciks == (2,)
    assert result.selected_filings == result.archived_filings == 1
    assert len(result.evidence) == 1 and not result.gaps
    assert result.evidence[0].filing.accession == ACCESSION
    master = build_cover_security_master(result.evidence, as_of=CUTOFF).master
    assert len(result.share_observations) == 1
    assert result.share_observations[0].shares_outstanding == 125_000_000
    assert result.share_observations[0].security_id == master.securities[0].security_id
    assert result.share_observations[0].accepted == datetime(2026, 5, 1, 20, tzinfo=UTC)
    assert master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"
    assert master.resolve("ONE", "NASDAQ", date(2026, 4, 1)).status != "mapped"
    assert tuple(tmp_path.glob(f"accessions/{ACCESSION}/snapshots/*/{PRIMARY}"))


def test_cover_acquisition_rejects_a_cover_pair_that_does_not_match_discovery(
    tmp_path: Path,
) -> None:
    result = acquire_cover_evidence(
        FakeClient("WRONG"),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert not result.evidence
    assert not result.share_observations
    assert result.gaps[0].kind == "cover_not_in_discovery_plan"


def test_cover_acquisition_quarantines_a_missing_historical_filing_resource(
    tmp_path: Path,
) -> None:
    class MissingArchiveClient(FakeClient):
        def filing_resource(
            self,
            cik: int,
            accession: str,
            filename: str,
            *,
            refresh: bool = False,
        ) -> EdgarResource:
            raise EdgarHttpError(404, "https://www.sec.gov/Archives/missing", "missing")

    result = acquire_cover_evidence(
        MissingArchiveClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert not result.evidence and not result.share_observations and result.archived_filings == 0
    assert result.gaps[0].kind == "filing_resource_missing"


def test_cover_acquisition_records_incomplete_current_symbols_without_losing_filings(
    tmp_path: Path,
) -> None:
    class IncompleteCurrentSymbolClient(FakeClient):
        def submissions(self, cik: int, *, refresh: bool) -> EdgarDocument:
            payload = _filing_payload()
            payload["tickers"] = ["ONE", "BROKEN"]
            payload["exchanges"] = ["Nasdaq", None]
            return _document(
                payload,
                "https://data.sec.gov/submissions/CIK0000000001.json",
            )

    result = acquire_cover_evidence(
        IncompleteCurrentSymbolClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert len(result.evidence) == len(result.share_observations) == 1
    assert [gap.kind for gap in result.gaps] == ["unusable_current_symbol_rows"]


def test_cover_acquisition_archives_complete_fpi_form_history_proof(tmp_path: Path) -> None:
    history_name = "CIK0000000001-submissions-001.json"

    class HistoryClient(FakeClient):
        def submissions(self, cik: int, *, refresh: bool) -> EdgarDocument:
            payload = _filing_payload()
            payload["filings"]["files"] = [{"name": history_name}]  # type: ignore[index]
            return _document(
                payload,
                "https://data.sec.gov/submissions/CIK0000000001.json",
            )

        def submission_history(self, filename: str, *, refresh: bool) -> EdgarDocument:
            assert filename == history_name and not refresh
            return _document(
                {
                    "accessionNumber": ["0000000001-20-000001"],
                    "acceptanceDateTime": ["2020-03-01T20:00:00Z"],
                    "form": ["20-F"],
                    "primaryDocument": [None],
                },
                f"https://data.sec.gov/submissions/{history_name}",
            )

    result = acquire_cover_evidence(
        HistoryClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert [row.form for row in result.fpi_form_observations] == ["20-F"]
    assert len(result.form_history_proofs) == 1
    assert len(result.form_history_proofs[0].source_documents) == 2


def test_filer_regime_uses_only_forms_accepted_by_the_cutoff() -> None:
    domestic = SubmissionFiling(
        1,
        ACCESSION,
        "10-Q",
        date(2026, 5, 1),
        datetime(2026, 5, 1, 20, tzinfo=UTC),
        date(2026, 3, 31),
        PRIMARY,
        "sec://submissions",
        "a" * 64,
    )
    future_foreign = SubmissionFiling(
        1,
        "0000000001-26-000002",
        "20-F",
        date(2026, 7, 18),
        datetime(2026, 7, 18, 20, tzinfo=UTC),
        date(2025, 12, 31),
        "future.htm",
        "sec://submissions",
        "a" * 64,
    )

    assert infer_domestic_flag((domestic, future_foreign), as_of=CUTOFF) is True
