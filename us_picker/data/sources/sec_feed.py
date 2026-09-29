"""SEC current-report feed (the KAP disclosure feed of BIST Picker).

For a held ticker, lists its latest material filings (8-K current reports and
their amendments, plus 10-Q/10-K and activist 13D) from EDGAR submissions and
extracts readable text from the primary document so the event analyzer can
summarize it.  Same interface as the old ``KAPFeed``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_WATCHED_FORMS = ("8-K", "8-K/A", "10-Q", "10-K", "SC 13D", "SC 13D/A")
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"
_ITEM_TITLES = {
    "1.01": "Material agreement",
    "1.02": "Termination of material agreement",
    "1.03": "Bankruptcy or receivership",
    "2.01": "Acquisition or disposition completed",
    "2.02": "Results of operations",
    "2.03": "New financial obligation",
    "2.05": "Exit or restructuring costs",
    "2.06": "Material impairment",
    "3.01": "Delisting notice",
    "4.01": "Auditor change",
    "4.02": "Non-reliance on prior financials",
    "5.02": "Officer or director change",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
}


def _cik_from_db(ticker: str) -> Optional[int]:
    from us_picker.db.connection import get_session
    from us_picker.db.schema import Company

    session = get_session()
    try:
        row = session.query(Company.cik).filter(Company.ticker == ticker).first()
        return int(row[0]) if row and row[0] else None
    finally:
        session.close()


class SECFilingFeed:
    """Latest material filings per ticker, KAPFeed-compatible."""

    def __init__(self, client=None, cik_lookup: Callable[[str], Optional[int]] = _cik_from_db):
        self._client = client
        self._cik_lookup = cik_lookup
        self._urls: dict[str, str] = {}

    @property
    def client(self):
        if self._client is None:
            from us_picker.data.sources.sec import SECClient

            self._client = SECClient()
        return self._client

    def get_latest_disclosures(self, ticker: str, limit: int = 5) -> list[dict]:
        ticker = ticker.upper().strip()
        cik = self._cik_lookup(ticker)
        if not cik:
            return []
        try:
            submissions = self.client.fetch_submissions(cik) or {}
        except Exception as exc:
            logger.error("SEC submissions failed for %s: %s", ticker, exc)
            return []
        recent = (submissions.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        result: list[dict] = []
        for i, form in enumerate(forms):
            if form not in _WATCHED_FORMS:
                continue
            accn = recent["accessionNumber"][i]
            doc = recent["primaryDocument"][i]
            items = recent.get("items", [""] * len(forms))[i] or ""
            url = _ARCHIVE_URL.format(cik=int(cik), accn=accn.replace("-", ""), doc=doc)
            self._urls[accn] = url
            try:
                filed = datetime.fromisoformat(recent["acceptanceDateTime"][i].replace("Z", "+00:00"))
            except (KeyError, ValueError, IndexError):
                filed = datetime.now()
            result.append(
                {
                    "date": filed,
                    "title": filing_title(form, items),
                    "url": url,
                    "id": accn,
                    "ticker": ticker,
                }
            )
            if len(result) >= limit:
                break
        return result

    def get_disclosure_text(self, disclosure_id: str) -> str:
        url = self._urls.get(disclosure_id)
        if not url:
            return ""
        try:
            response = self.client._get(url)
        except Exception as exc:
            logger.error("SEC document fetch failed for %s: %s", disclosure_id, exc)
            return ""
        if response is None:
            return ""
        return document_text(response.text)


def filing_title(form: str, items: str) -> str:
    codes = [code.strip() for code in str(items).split(",") if code.strip()]
    labels = [_ITEM_TITLES.get(code, f"Item {code}") for code in codes]
    return f"{form}: {', '.join(labels)}" if labels else form


def document_text(html: str, max_chars: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [line.strip() for line in re.sub(r"\n+", "\n", text).split("\n") if line.strip()]
    return "\n".join(lines)[:max_chars]
