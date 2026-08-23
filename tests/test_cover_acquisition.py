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


def _inline_xbrl(ticker: str = "ONE", exchange: str = "NASDAQ") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:dei="http://xbrl.sec.gov/dei/2025">
 <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
 <xbrli:context id="cover">
  <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
 </xbrli:context>
 <dei:TradingSymbol contextRef="cover">{ticker}</dei:TradingSymbol>
 <dei:SecurityExchangeName contextRef="cover">{exchange}</dei:SecurityExchangeName>
 <dei:Security12bTitle contextRef="cover">Common Stock</dei:Security12bTitle>
 <dei:EntityCommonStockSharesOutstanding contextRef="cover" unitRef="shares" decimals="0">
  125000000
 </dei:EntityCommonStockSharesOutstanding>
</xbrli:xbrl>
""".encode()


def _inline_xbrl_without_listing_tags(title: str = "Common Stock") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:dei="http://xbrl.sec.gov/dei/2025">
 <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
 <xbrli:context id="cover">
  <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">1</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
 </xbrli:context>
 <dei:Security12bTitle contextRef="cover">{title}</dei:Security12bTitle>
 <dei:EntityCommonStockSharesOutstanding contextRef="cover" unitRef="shares" decimals="0">
  125000000
 </dei:EntityCommonStockSharesOutstanding>
</xbrli:xbrl>
""".encode()


class FakeClient:
    def __init__(self, ticker: str = "ONE", exchange: str = "NASDAQ") -> None:
        self.primary = _inline_xbrl(ticker, exchange)
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


def test_name_discovered_candidate_keeps_fsds_provenance_until_cover_match(
    tmp_path: Path,
) -> None:
    row = FilingDiscoveryRow(
        "ONE",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://one/1",
        "discovered",
        (1,),
        ("sec-fsds://source/accession?cik=1#issuer-name",),
    )
    plan = FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, (row,))

    result = acquire_cover_evidence(FakeClient(), plan, tmp_path, as_of=CUTOFF)
    master = build_cover_security_master(result.evidence, as_of=CUTOFF).master

    pointer = result.evidence[0].allowed_pair_evidence[0][2]
    assert "alpha-vantage://one/1" in pointer
    assert "sec-fsds://source/accession?cik=1#issuer-name" in pointer
    assert "sec-company-tickers-exchange://" not in pointer
    assert "TradingSymbol" in master.symbols[0].evidence_pointer


def test_strong_entity_candidate_admits_sec_cover_ticker_expansion(tmp_path: Path) -> None:
    row = FilingDiscoveryRow(
        "OLD",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://old/1",
        "discovered",
        (1,),
        ("sec-fsds://source/accession?cik=1#issuer-name",),
    )
    plan = FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, (row,))

    result = acquire_cover_evidence(
        FakeClient("NEW"),
        plan,
        tmp_path,
        as_of=CUTOFF,
    )
    master = build_cover_security_master(result.evidence, as_of=CUTOFF).master

    assert not result.gaps
    assert master.resolve("NEW", "NASDAQ", date(2026, 6, 1)).status == "mapped"
    assert master.resolve("OLD", "NASDAQ", date(2026, 6, 1)).status == "unmapped"


def test_exact_same_filing_index_can_supply_missing_cover_listing_tags(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.primary = _inline_xbrl_without_listing_tags()
    filing_index_pointer = (
        "https://efts.sec.gov/LATEST/search-index?q=ONE"
        f"#filing-index-{ACCESSION}-cik-1;sha256={'c' * 64};ticker=ONE"
    )
    row = FilingDiscoveryRow(
        "ONE",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://one/1",
        "discovered",
        (1,),
        (filing_index_pointer,),
    )
    plan = FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, (row,))

    result = acquire_cover_evidence(client, plan, tmp_path, as_of=CUTOFF)
    bootstrap = build_cover_security_master(result.evidence, as_of=CUTOFF)

    assert not result.gaps and not bootstrap.gaps
    assert bootstrap.master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"
    assert filing_index_pointer in bootstrap.master.symbols[0].evidence_pointer


def test_stale_filing_index_pointer_cannot_supply_missing_cover_listing_tags(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.primary = _inline_xbrl_without_listing_tags()
    row = FilingDiscoveryRow(
        "ONE",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://one/1",
        "discovered",
        (1,),
        (
            "https://efts.sec.gov/LATEST/search-index?q=ONE"
            f"#filing-index-0000000001-25-999999-cik-1;sha256={'c' * 64};ticker=ONE",
        ),
    )
    plan = FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, (row,))

    result = acquire_cover_evidence(client, plan, tmp_path, as_of=CUTOFF)

    assert not result.evidence
    assert result.gaps[0].kind == "cover_not_in_discovery_plan"


def test_same_filing_ticker_evidence_does_not_promote_a_preferred_class(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    client.primary = _inline_xbrl_without_listing_tags("Series A Preferred Stock")
    row = FilingDiscoveryRow(
        "ONE",
        "NASDAQ",
        "NASDAQ",
        "Stock",
        "alpha-vantage://one/1",
        "discovered",
        (1,),
        (
            "https://efts.sec.gov/LATEST/search-index?q=ONE"
            f"#filing-index-{ACCESSION}-cik-1;sha256={'c' * 64};ticker=ONE",
        ),
    )
    plan = FilingDiscoveryPlan(date(2026, 7, 17), "a" * 64, "b" * 64, OBSERVED, (row,))

    result = acquire_cover_evidence(client, plan, tmp_path, as_of=CUTOFF)

    assert not result.evidence
    assert result.gaps[0].kind == "cover_not_in_discovery_plan"


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


def test_cover_acquisition_falls_back_to_an_archived_instance_document(tmp_path: Path) -> None:
    class InstanceFallbackClient(FakeClient):
        def filing_resource(
            self,
            cik: int,
            accession: str,
            filename: str,
            *,
            refresh: bool = False,
        ) -> EdgarResource:
            assert cik == 1 and accession == ACCESSION and not refresh
            if filename == "index.json":
                body = json.dumps(
                    {"directory": {"item": [{"name": PRIMARY}, {"name": "issuer-20260331.xml"}]}}
                ).encode()
            elif filename == PRIMARY:
                body = b"<html><body>ordinary filing document</body></html>"
            else:
                assert filename == "issuer-20260331.xml"
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

    result = acquire_cover_evidence(
        InstanceFallbackClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert len(result.evidence) == 1
    assert not result.gaps
    assert result.evidence[0].parsed.facts[0].source_document == "issuer-20260331.xml"


def test_cover_acquisition_normalizes_official_nasdaq_cover_label(tmp_path: Path) -> None:
    result = acquire_cover_evidence(
        FakeClient(exchange="The Nasdaq Global Select Market"),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    master = build_cover_security_master(result.evidence, as_of=CUTOFF).master
    assert not result.gaps
    assert master.resolve("ONE", "NASDAQ", date(2026, 6, 1)).status == "mapped"


def test_cover_acquisition_does_not_reconcile_unrelated_exchanges(tmp_path: Path) -> None:
    result = acquire_cover_evidence(
        FakeClient(exchange="NYSE"),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    assert not result.evidence
    assert result.gaps[0].kind == "cover_not_in_discovery_plan"


def test_cover_acquisition_reconciles_contextual_nyse_american_label(tmp_path: Path) -> None:
    base = _plan()
    row = base.rows[0]
    plan = FilingDiscoveryPlan(
        base.listing_as_of,
        base.listing_snapshot_id,
        base.association_source_sha256,
        base.association_observed_at,
        (
            FilingDiscoveryRow(
                row.ticker,
                "NYSE American",
                "NYSEAMERICAN",
                row.asset_type,
                row.listing_evidence_pointer,
                row.status,
                row.candidate_ciks,
            ),
        ),
    )
    result = acquire_cover_evidence(
        FakeClient(exchange="NYSE"),
        plan,
        tmp_path,
        as_of=CUTOFF,
    )

    bootstrap = build_cover_security_master(result.evidence, as_of=CUTOFF)
    assert not result.gaps and not bootstrap.gaps
    mapping = bootstrap.master.resolve("ONE", "NYSEAMERICAN", date(2026, 6, 1))
    assert mapping.status == "mapped"
    symbol = next(row for row in bootstrap.master.symbols if row.security_id == mapping.security_id)
    assert symbol.source == "sec_xbrl_cover+listing_discovery"
    assert "sec-company-tickers-exchange://" in symbol.evidence_pointer


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


def test_complete_form_history_records_effective_terminal_form(tmp_path: Path) -> None:
    class TerminalClient(FakeClient):
        def submissions(self, cik: int, *, refresh: bool) -> EdgarDocument:
            payload = _filing_payload()
            recent = payload["filings"]["recent"]  # type: ignore[index]
            recent["accessionNumber"][1] = "0000000001-26-000003"  # type: ignore[index]
            recent["filingDate"][1] = "2026-06-01"  # type: ignore[index]
            recent["acceptanceDateTime"][1] = "2026-06-01T20:00:00Z"  # type: ignore[index]
            recent["form"][1] = "15-12B"  # type: ignore[index]
            recent["primaryDocument"][1] = ""  # type: ignore[index]
            recent["reportDate"][1] = ""  # type: ignore[index]
            return _document(
                payload,
                "https://data.sec.gov/submissions/CIK0000000001.json",
            )

    result = acquire_cover_evidence(
        TerminalClient(),
        _plan(),
        tmp_path,
        as_of=CUTOFF,
        maximum_ciks=1,
    )

    terminal = result.form_history_proofs[0].terminal_form_observations
    assert len(terminal) == 1
    assert terminal[0].form == "15-12B"
    assert terminal[0].accepted == datetime(2026, 6, 1, 20, tzinfo=UTC)


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
