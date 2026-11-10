from datetime import date
from types import SimpleNamespace

import pytest
import requests

from us_picker.data.sources.sec import SECClient


def _master_index(last_received: str, *rows: str) -> str:
    return "\n".join(
        [
            f"Last Data Received:    {last_received}",
            "CIK|Company Name|Form Type|Date Filed|Filename",
            "-" * 80,
            *rows,
        ]
    )


def _denied(url: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = 403
    response.url = url
    return requests.HTTPError("SEC denied access", response=response)


def test_daily_403_uses_dated_master_indexes_across_quarters(monkeypatch):
    monkeypatch.setenv("US_PICKER_SEC_EMAIL", "fixtures@example.com")
    client = SECClient()
    urls = []

    def get(url):
        urls.append(url)
        if url.endswith("form.20260929.idx"):
            return SimpleNamespace(text=f"{'10-K':<12}First 111 2026-09-29 filing.txt")
        if "/daily-index/" in url:
            raise _denied(url)
        if "/QTR3/" in url:
            return SimpleNamespace(
                text=_master_index(
                    "September 30, 2026",
                    "222|Old|10-K|2026-09-28|old.txt",
                    "333|On boundary|10-Q|2026-09-30|current.txt",
                )
            )
        return SimpleNamespace(
            text=_master_index(
                "October 5, 2026",
                "444|Amendment|10-K/A|2026-10-01|amendment.txt",
                "555|Other form|8-K|2026-10-01|other.txt",
                "666|Last day|10-QT|2026-10-02|last.txt",
                "777|Future|10-K|2026-10-05|future.txt",
            )
        )

    monkeypatch.setattr(client, "_get", get)
    assert client.recent_periodic_filers(date(2026, 9, 29), date(2026, 10, 2)) == {
        111,
        333,
        444,
        666,
    }
    assert [url.split("/edgar/")[1] for url in urls] == [
        "daily-index/2026/QTR3/form.20260929.idx",
        "daily-index/2026/QTR3/form.20260930.idx",
        "full-index/2026/QTR3/master.idx",
        "full-index/2026/QTR4/master.idx",
    ]


@pytest.mark.parametrize(
    "fallback", [None, "<html>blocked</html>", _master_index("November 6, 2026")]
)
def test_missing_or_incomplete_master_preserves_403_for_submissions_fallback(monkeypatch, fallback):
    monkeypatch.setenv("US_PICKER_SEC_EMAIL", "fixtures@example.com")
    client = SECClient()
    denied = _denied("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261109.idx")

    def get(url):
        if "/daily-index/" in url:
            raise denied
        return None if fallback is None else SimpleNamespace(text=fallback)

    monkeypatch.setattr(client, "_get", get)
    with pytest.raises(requests.HTTPError) as exc:
        client.recent_periodic_filers(date(2026, 11, 9), date(2026, 11, 9))
    assert exc.value is denied
