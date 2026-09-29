from __future__ import annotations

from datetime import date

import requests

from us_picker.data.sources.isyatirim import (
    _PRICE_CIRCUIT_FAILURE_LIMIT,
    IsYatirimClient,
)


def test_price_circuit_skips_primary_after_consecutive_outage(monkeypatch) -> None:
    client = IsYatirimClient(verify_ssl=False)
    calls = {"count": 0}

    def fail_request(*args, **kwargs):
        calls["count"] += 1
        raise requests.ConnectTimeout("provider unavailable")

    monkeypatch.setattr(client, "_get", fail_request)

    for index in range(_PRICE_CIRCUIT_FAILURE_LIMIT + 5):
        result = client.fetch_price_data(
            f"TEST{index}",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 12),
        )
        assert result.empty

    assert calls["count"] == _PRICE_CIRCUIT_FAILURE_LIMIT
    assert client._price_circuit_open.is_set()


def test_probe_timeout_opens_price_circuit_immediately(monkeypatch) -> None:
    def fail_probe(*args, **kwargs):
        raise requests.ConnectTimeout("provider unavailable")

    monkeypatch.setattr(requests.Session, "get", fail_probe)
    client = IsYatirimClient()
    primary_calls = {"count": 0}

    def unexpected_primary_call(*args, **kwargs):
        primary_calls["count"] += 1
        raise AssertionError("primary price endpoint should be bypassed")

    monkeypatch.setattr(client, "_get", unexpected_primary_call)
    result = client.fetch_price_data(
        "TEST",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 12),
    )

    assert result.empty
    assert client._price_circuit_open.is_set()
    assert primary_calls["count"] == 0
