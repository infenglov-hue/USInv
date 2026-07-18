from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from tools import historical_feasibility as feasibility

RESEARCH_DIR = Path(__file__).resolve().parents[1] / "research" / "phase_0_4"


def _documents() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    return tuple(
        json.loads((RESEARCH_DIR / name).read_text(encoding="utf-8"))
        for name in ("awkward_securities.json", "provider_contracts.json", "observations.json")
    )


def test_committed_feasibility_inputs_cover_all_strata() -> None:
    sample_document, contract_document, observation_document = _documents()
    securities = feasibility.validate_sample_manifest(sample_document)
    providers = feasibility.validate_provider_contracts(contract_document)
    observations = feasibility.validate_observations(
        observation_document, {provider["provider"] for provider in providers}
    )

    assert len(securities) == 36
    assert {security["stratum"] for security in securities} == feasibility.REQUIRED_STRATA
    assert {provider["provider"] for provider in providers} == {
        "eodhd",
        "alpha_vantage",
        "crsp",
    }
    assert len(observations) == 43


def test_sample_manifest_rejects_bare_ticker_security_key() -> None:
    sample_document, _contracts, _observations = _documents()
    invalid = copy.deepcopy(sample_document)
    invalid["securities"][0]["security_key"] = "AAPL"

    with pytest.raises(feasibility.FeasibilityError, match="namespaced"):
        feasibility.validate_sample_manifest(invalid)


def test_sample_manifest_rejects_missing_required_stratum() -> None:
    sample_document, _contracts, _observations = _documents()
    invalid = copy.deepcopy(sample_document)
    invalid["securities"] = [
        security for security in invalid["securities"] if security["stratum"] != "pre_2018_delisted"
    ]

    with pytest.raises(feasibility.FeasibilityError, match="missing strata"):
        feasibility.validate_sample_manifest(invalid)


def test_provider_contract_requires_every_field() -> None:
    _samples, contract_document, _observations = _documents()
    invalid = copy.deepcopy(contract_document)
    del invalid["providers"][0]["capabilities"]["delisting_reason"]

    with pytest.raises(feasibility.FeasibilityError, match="every coverage field"):
        feasibility.validate_provider_contracts(invalid)


def test_observations_cannot_claim_a_tracked_raw_archive() -> None:
    _samples, contract_document, observation_document = _documents()
    providers = feasibility.validate_provider_contracts(contract_document)
    invalid = copy.deepcopy(observation_document)
    invalid["observations"][0]["raw_archive"] = "research/phase_0_4/raw.json"

    with pytest.raises(feasibility.FeasibilityError, match="local_artifact_only"):
        feasibility.validate_observations(invalid, {provider["provider"] for provider in providers})


def test_sample_batch_rejects_tampered_manifest_hash() -> None:
    _samples, contract_document, observation_document = _documents()
    providers = feasibility.validate_provider_contracts(contract_document)
    invalid = copy.deepcopy(observation_document)
    invalid["sample_batches"][0]["manifest_sha256"] = "tampered"

    with pytest.raises(feasibility.FeasibilityError, match="invalid hash"):
        feasibility.validate_observations(invalid, {provider["provider"] for provider in providers})


def test_matrix_only_promotes_sample_scoped_observations() -> None:
    sample_document, contract_document, observation_document = _documents()
    securities = feasibility.validate_sample_manifest(sample_document)
    providers = feasibility.validate_provider_contracts(contract_document)
    observations = feasibility.validate_observations(
        observation_document, {provider["provider"] for provider in providers}
    )

    matrix = feasibility.build_matrix(securities, providers, observations)
    aapl_eodhd = next(
        row for row in matrix if row["provider"] == "eodhd" and row["sample_id"] == "active_aapl"
    )
    msft_eodhd = next(
        row for row in matrix if row["provider"] == "eodhd" and row["sample_id"] == "active_msft"
    )
    aapl_alpha = next(
        row
        for row in matrix
        if row["provider"] == "alpha_vantage" and row["sample_id"] == "active_aapl"
    )
    fnma_alpha = next(
        row
        for row in matrix
        if row["provider"] == "alpha_vantage" and row["sample_id"] == "otc_fnma"
    )

    assert aapl_eodhd["raw_ohlcv"] == "observed"
    assert aapl_eodhd["splits"] == "observed"
    assert aapl_eodhd["dividends"] == "observed"
    assert aapl_eodhd["identifier_mapping"] == "blocked"
    assert msft_eodhd["raw_ohlcv"] == "documented"
    assert msft_eodhd["splits"] == "documented_gap"
    assert aapl_alpha["listing_date"] == "observed"
    assert aapl_alpha["delisting_date"] == "observed_gap"
    assert fnma_alpha["listing_date"] == "observed_gap"


def test_alpha_validator_rejects_post_cutoff_delisting() -> None:
    content = (
        b"symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
        b"LEAK,Future Corp,NASDAQ,Stock,2010-01-01,2014-07-11,Delisted\n"
    )
    validator = feasibility._alpha_validator(
        expected_status="Delisted", cutoff=feasibility.date(2014, 7, 10)
    )

    valid, _shape, _fields, _row_count, notes = validator(200, content)

    assert not valid
    assert "post-cutoff" in notes


def test_render_writes_metadata_only_matrix(tmp_path: Path) -> None:
    (
        sample_document,
        contract_document,
        observation_document,
        securities,
        providers,
        observations,
    ) = feasibility._validated_inputs(RESEARCH_DIR)
    matrix = feasibility.build_matrix(securities, providers, observations)
    manifest_hash = feasibility._input_hash(
        sample_document, contract_document, observation_document
    )

    feasibility.render_outputs(
        matrix,
        securities,
        providers,
        observations,
        output_dir=tmp_path,
        manifest_hash=manifest_hash,
    )

    summary = json.loads((tmp_path / "coverage_summary.json").read_text(encoding="utf-8"))
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert summary["security_count"] == 36
    assert summary["matrix_row_count"] == 108
    assert summary["gate_status"] == "blocked_pending_research_archive_rights_and_sample"
    assert "No honest historical" in report
    assert not (tmp_path / "raw").exists()


def test_live_demo_capture_is_redacted_and_uses_explicit_tmp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_header = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"

    def fake_request(url: str, *, timeout: float) -> tuple[int, dict[str, str], bytes]:
        assert timeout == 5.0
        if "/eod/" in url:
            payload: object = [
                {
                    "date": "2020-08-28",
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "adjusted_close": 0.5,
                    "volume": 10,
                }
            ]
        elif "/splits/" in url:
            payload = [{"date": "2020-08-31", "split": "4.000000/1.000000"}]
        elif "/div/" in url:
            payload = [{"date": "2020-08-07", "value": 0.82}]
        elif "id-mapping" in url or "exchange-symbol-list" in url:
            return 403, {"Content-Type": "text/plain"}, b"blocked"
        elif "state=delisted" in url:
            return (
                200,
                {"Content-Type": "text/csv"},
                (csv_header + "OLD,Old Corp,NYSE,Stock,2000-01-01,2014-07-09,Delisted\n").encode(),
            )
        elif "state=active" in url:
            return (
                200,
                {"Content-Type": "text/csv"},
                (csv_header + "LIVE,Live Corp,NASDAQ,Stock,2014-01-01,,Active\n").encode(),
            )
        else:
            raise AssertionError(url)
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    monkeypatch.setattr(feasibility, "_request", fake_request)

    captures = feasibility.run_public_demo(tmp_path, provider="all", timeout=5.0)

    assert len(captures) == 7
    assert sum(capture.valid for capture in captures) == 5
    manifest_text = (tmp_path / "capture_manifest.json").read_text(encoding="utf-8")
    assert "api_token=demo" not in manifest_text
    assert "apikey=demo" not in manifest_text
    assert "REDACTED" in manifest_text
    assert (tmp_path / "eodhd" / "aapl_eod_2020_split_window.json").exists()


def test_live_demo_refuses_tracked_source_directory() -> None:
    with pytest.raises(feasibility.FeasibilityError, match="ignored artifacts"):
        feasibility.run_public_demo(RESEARCH_DIR, provider="eodhd")


def test_full_alpha_sample_matches_symbols_without_leaking_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_document, _contracts, _observations = _documents()
    securities = feasibility.validate_sample_manifest(sample_document)[:2]
    header = "symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"

    def fake_request(url: str, *, timeout: float) -> tuple[int, dict[str, str], bytes]:
        assert "private-alpha-key" in url
        assert timeout == 4.0
        if "state=active" in url:
            content = header + "AAPL,Apple Inc.,NASDAQ,Stock,1980-12-12,,Active\n"
        else:
            content = header + "OLD,Old Corp,NYSE,Stock,2000-01-01,2020-01-01,Delisted\n"
        return 200, {"Content-Type": "text/csv"}, content.encode()

    monkeypatch.setattr(feasibility, "_request", fake_request)
    sleep_calls: list[float] = []
    monkeypatch.setattr(feasibility.time, "sleep", sleep_calls.append)

    captures = feasibility.run_alpha_vantage_sample(
        tmp_path,
        securities=securities,
        api_key="private-alpha-key",
        as_of=date(2024, 1, 31),
        timeout=4.0,
    )

    assert len(captures) == 2
    assert sleep_calls == [15.0]
    manifest_text = (tmp_path / "capture_manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert "private-alpha-key" not in manifest_text
    assert manifest["sample_coverage"][0]["matched_symbols"] == ["AAPL"]
    assert manifest["sample_coverage"][0]["fields"]["listing_date"] == "observed"
    assert manifest["sample_coverage"][0]["fields"]["delisting_date"] == "observed_gap"
    assert manifest["sample_coverage"][1]["fields"]["listing_date"] == "observed_gap"


def test_full_eodhd_sample_is_bounded_and_does_not_overclaim_lifecycle_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_document, _contracts, _observations = _documents()
    security = feasibility.validate_sample_manifest(sample_document)[0]

    def fake_request(url: str, *, timeout: float) -> tuple[int, dict[str, str], bytes]:
        assert "private-eod-key" in url
        assert timeout == 6.0
        if "exchange-symbol-list" in url:
            payload: object = [{"Code": "OLD", "Exchange": "NYSE", "Type": "Common Stock"}]
        elif "/eod/" in url:
            assert "from=2023-12-17" in url and "to=2024-01-31" in url
            payload = [
                {
                    "date": "2024-01-02",
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "adjusted_close": 2,
                    "volume": 10,
                }
            ]
        elif "/splits/" in url:
            payload = [{"date": "2024-01-15", "split": "2/1"}]
        elif "/div/" in url:
            payload = [{"date": "2024-01-15", "value": 0.1}]
        elif "id-mapping" in url:
            payload = {"data": [{"symbol": "AAPL.US", "cik": "0000320193"}]}
        else:
            raise AssertionError(url)
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()

    monkeypatch.setattr(feasibility, "_request", fake_request)

    captures = feasibility.run_eodhd_sample(
        tmp_path,
        securities=[security],
        api_token="private-eod-key",
        as_of=date(2024, 1, 31),
        timeout=6.0,
    )

    assert len(captures) == 5
    manifest_text = (tmp_path / "capture_manifest.json").read_text(encoding="utf-8")
    coverage = json.loads(manifest_text)["sample_coverage"][0]
    assert "private-eod-key" not in manifest_text
    assert coverage["selected_symbol"] == "AAPL.US"
    assert coverage["fields"]["raw_ohlcv"] == "observed"
    assert coverage["fields"]["historical_exchange_security_type"] == "observed"
    assert coverage["fields"]["listing_date"] == "observed_gap"
    assert coverage["fields"]["delisting_date"] == "observed_gap"


def test_live_sample_fails_before_network_without_provider_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("EODHD_API_TOKEN", raising=False)

    assert (
        feasibility.main(
            [
                "live-sample",
                "--provider",
                "eodhd",
                "--as-of",
                "2024-01-31",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 2
    )
    assert "requires EODHD_API_TOKEN" in capsys.readouterr().err


def test_validate_cli_reports_stable_counts(capsys: pytest.CaptureFixture[str]) -> None:
    assert feasibility.main(["validate", "--research-dir", str(RESEARCH_DIR)]) == 0
    output = capsys.readouterr().out
    assert "securities=36 providers=3 observations=43 rows=108" in output
