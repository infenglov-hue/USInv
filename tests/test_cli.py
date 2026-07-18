from datetime import UTC, datetime

import pytest

from usinv.cli import main
from usinv.data.edgar.client import EdgarClient, EdgarDocument


def _document(payload: dict[str, object]) -> EdgarDocument:
    return EdgarDocument(
        url="https://data.sec.gov/fixture",
        retrieved_at=datetime(2026, 7, 18, tzinfo=UTC),
        validated_at=datetime(2026, 7, 18, tzinfo=UTC),
        content_sha256="a" * 64,
        payload=payload,
        from_cache=False,
        revalidated=False,
    )


def test_config_check_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config-check"]) == 0
    output = capsys.readouterr().out
    assert "config_ok schema=1" in output
    assert "evidence=research execution=paper holdings=15 overlay=O0" in output


def test_edgar_smoke_fails_closed_without_contact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("USINV_EDGAR_EMAIL", raising=False)

    assert main(["edgar-smoke"]) == 2
    assert "set USINV_EDGAR_EMAIL" in capsys.readouterr().err


def test_edgar_smoke_reports_both_documents_without_dumping_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        def submissions(self, cik: str, *, refresh: bool) -> EdgarDocument:
            assert cik == "0000320193" and refresh
            return _document({"name": "Apple Inc.", "filings": {}, "cik": "320193"})

        def companyfacts(self, cik: str, *, refresh: bool) -> EdgarDocument:
            assert cik == "0000320193" and refresh
            return _document({"entityName": "Apple Inc.", "facts": {}, "cik": 320193})

    monkeypatch.setattr(
        EdgarClient,
        "from_config",
        staticmethod(lambda config, *, cache_dir=None: FakeClient()),
    )

    assert main(["edgar-smoke", "--refresh"]) == 0
    output = capsys.readouterr().out
    assert "edgar_smoke_ok cik=0000320193 name='Apple Inc.'" in output
    assert "submissions_sha256=" in output and "companyfacts_sha256=" in output
    assert "accessionNumber" not in output
