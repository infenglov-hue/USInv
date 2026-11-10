"""SEC EDGAR client: company list, submissions, companyfacts, filing index.

EDGAR requires a descriptive User-Agent with a contact e-mail and allows about
10 requests per second.  The e-mail comes from ``US_PICKER_SEC_EMAIL`` (or the
legacy ``USINV_EDGAR_EMAIL``) and is only ever sent to sec.gov.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from us_picker.utils.rate_limiter import RateLimiter

logger = logging.getLogger("us_picker.data.sources.sec")

_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_BULK_COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
_DAILY_FORM_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{ymd}.idx"
)
_FULL_MASTER_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/master.idx"
_MASTER_INDEX_HEADER = "CIK|Company Name|Form Type|Date Filed|Filename"
_PERIODIC_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT")
_NEW_YORK = ZoneInfo("America/New_York")


def latest_index_date() -> date:
    """EDGAR publishes the current day's daily index after its evening close."""
    return datetime.now(_NEW_YORK).date() - timedelta(days=1)


def sec_contact_email() -> str:
    email = (
        os.environ.get("US_PICKER_SEC_EMAIL", "").strip()
        or os.environ.get("USINV_EDGAR_EMAIL", "").strip()
    )
    if not email:
        raise RuntimeError(
            "SEC EDGAR needs a contact e-mail: set US_PICKER_SEC_EMAIL."
        )
    return email


class SECClient:
    """Thin, polite EDGAR client with retries."""

    def __init__(
        self,
        rate_limiter: Optional[RateLimiter] = None,
        session: Optional[requests.Session] = None,
        max_retries: int = 5,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter(min_delay=0.12, name="sec")
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._headers = {
            "User-Agent": f"USPicker research {sec_contact_email()}",
            "Accept-Encoding": "gzip, deflate",
        }

    # -- transport ---------------------------------------------------------

    def _get(self, url: str, *, stream: bool = False, timeout: int = 60) -> Optional[requests.Response]:
        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.wait()
            try:
                response = self._session.get(
                    url, headers=self._headers, timeout=timeout, stream=stream
                )
            except requests.RequestException as exc:
                logger.warning("SEC request failed (%s/%s) %s: %s", attempt, self._max_retries, url, exc)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code == 404:
                return None
            if response.status_code in (429, 500, 502, 503, 504):
                logger.warning("SEC %s on %s (attempt %s)", response.status_code, url, attempt)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"SEC request kept failing: {url}")

    def _get_json(self, url: str) -> Optional[dict]:
        response = self._get(url)
        return None if response is None else response.json()

    # -- endpoints ---------------------------------------------------------

    def fetch_company_tickers(self) -> pd.DataFrame:
        """All SEC-registered tickers with CIK and listing exchange."""

        payload = self._get_json(_TICKERS_URL) or {}
        fields = payload.get("fields") or ["cik", "name", "ticker", "exchange"]
        df = pd.DataFrame(payload.get("data") or [], columns=fields)
        if df.empty:
            return df
        df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
        df["cik"] = df["cik"].astype(int)
        return df

    def fetch_submissions(self, cik: int) -> Optional[dict]:
        return self._get_json(_SUBMISSIONS_URL.format(cik=int(cik)))

    def fetch_companyfacts(self, cik: int) -> Optional[dict]:
        return self._get_json(_COMPANYFACTS_URL.format(cik=int(cik)))

    def recent_periodic_filers(self, since: date, until: Optional[date] = None) -> set[int]:
        """CIKs that filed a 10-K/10-Q (or amendment) in ``[since, until]``.

        Reads EDGAR daily form indexes; days without an index (weekends,
        holidays, not yet published) are skipped. If a daily index returns
        HTTP 403, try the official full indexes for the remaining dates.
        """

        until = until or latest_index_date()
        ciks: set[int] = set()
        day = since
        while day <= until:
            if day.weekday() < 5:
                url = _DAILY_FORM_INDEX_URL.format(
                    year=day.year, qtr=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d")
                )
                try:
                    response = self._get(url)
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 403:
                        raise
                    remaining = self._full_index_periodic_filers(day, until)
                    if remaining is None:
                        raise
                    return ciks | remaining
                if response is not None:
                    ciks |= parse_form_index(response.text)
            day += timedelta(days=1)
        return ciks

    def _full_index_periodic_filers(self, since: date, until: date) -> Optional[set[int]]:
        ciks: set[int] = set()
        day = since
        while day <= until:
            quarter = (day.month - 1) // 3 + 1
            if quarter == 4:
                next_quarter = date(day.year + 1, 1, 1)
            else:
                next_quarter = date(day.year, quarter * 3 + 1, 1)
            response = self._get(_FULL_MASTER_INDEX_URL.format(year=day.year, qtr=quarter))
            if response is None:
                return None
            quarter_end = next_quarter - timedelta(days=1)
            filers = _parse_master_index(response.text, day, min(until, quarter_end))
            if filers is None:
                return None
            ciks.update(filers)
            day = next_quarter
        return ciks

    def filed_periodic_report(self, cik: int, since: date, until: date) -> bool:
        """Check the official SEC submissions API when daily indexes are unavailable."""
        payload = self.fetch_submissions(cik)
        if payload is None:
            raise RuntimeError(f"SEC submissions not found for CIK {cik}")
        recent = payload["filings"]["recent"]
        return any(
            form in _PERIODIC_FORMS and since <= date.fromisoformat(filed) <= until
            for form, filed in zip(recent["form"], recent["filingDate"], strict=True)
        )

    def download_bulk_companyfacts(self, target: Path) -> Path:
        """Download EDGAR's nightly companyfacts.zip (~1.3 GB) to ``target``."""

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        response = self._get(_BULK_COMPANYFACTS_URL, stream=True, timeout=600)
        if response is None:
            raise RuntimeError("companyfacts.zip not found on EDGAR")
        with tmp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
        tmp.replace(target)
        return target


def parse_form_index(text: str) -> set[int]:
    """Extract CIKs of periodic filings from an EDGAR ``form.idx`` listing."""

    ciks: set[int] = set()
    for line in text.splitlines():
        form = line[:12].strip()
        if form not in _PERIODIC_FORMS:
            continue
        # Fixed-width layout: form, company, CIK, date, file name.
        parts = line.split()
        for token in reversed(parts):
            if token.isdigit() and len(token) <= 10:
                ciks.add(int(token))
                break
    return ciks


def _parse_master_index(text: str, since: date, until: date) -> Optional[set[int]]:
    """Read dated periodic forms from a complete official master index."""
    lines = iter(text.splitlines())
    last_received = None
    for line in lines:
        if line.startswith("Last Data Received:"):
            try:
                last_received = datetime.strptime(line.split(":", 1)[1].strip(), "%B %d, %Y").date()
            except ValueError:
                return None
        if line.strip() == _MASTER_INDEX_HEADER:
            break
    else:
        return None

    last_weekday = until
    while last_weekday.weekday() >= 5:
        last_weekday -= timedelta(days=1)
    if last_received is None or last_received < last_weekday:
        return None

    ciks: set[int] = set()
    for line in lines:
        fields = line.split("|", 4)
        if len(fields) == 5:
            cik, _, form, filed, _ = fields
            if form in _PERIODIC_FORMS:
                try:
                    if since <= date.fromisoformat(filed) <= until:
                        ciks.add(int(cik))
                except ValueError:
                    return None
    return ciks


def iter_bulk_companyfacts(zip_path: Path, ciks: Iterable[int]) -> Iterator[tuple[int, dict]]:
    """Yield ``(cik, companyfacts)`` for the requested CIKs from the bulk zip."""

    wanted = {f"CIK{int(c):010d}.json": int(c) for c in ciks}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            cik = wanted.get(name)
            if cik is None:
                continue
            with archive.open(name) as handle:
                yield cik, json.load(io.TextIOWrapper(handle, encoding="utf-8"))


def periodic_form(forms: list[str]) -> Optional[str]:
    """Newest periodic report family in a filer's recent filings."""
    for form in forms:
        base = form.split("/")[0]
        if base in ("10-K", "10-Q", "10-KT", "10-QT"):
            return "10-K"
        if base in ("20-F", "40-F"):
            return base
    return None


def company_profile(submissions: dict) -> dict:
    """The submissions fields the pipeline stores on ``companies``."""

    former = submissions.get("formerNames") or []
    forms = ((submissions.get("filings") or {}).get("recent") or {}).get("form") or []
    return {
        "periodic_form": periodic_form(forms),
        "name": submissions.get("name"),
        "sic": str(submissions.get("sic") or "") or None,
        "sic_description": submissions.get("sicDescription"),
        "fiscal_year_end": submissions.get("fiscalYearEnd"),
        "entity_type": submissions.get("entityType"),
        "filer_category": submissions.get("category"),
        "state_of_incorporation": submissions.get("stateOfIncorporation"),
        "tickers": submissions.get("tickers") or [],
        "exchanges": submissions.get("exchanges") or [],
        "former_names": [f.get("name") for f in former if f.get("name")],
    }
