"""S&P 500 membership (the BIST 100 role in the index-aware selector).

Source: the community-maintained ``fja05680/sp500`` dataset, which lists
every constituent's membership interval since 1996 as
``ticker,start_date,end_date`` (blank end = current member).  Tickers use the
dot share-class form (``BRK.B``) like Alpaca.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger("us_picker.data.sources.sp500")

_INTERVALS_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)


@dataclass(frozen=True)
class MembershipInterval:
    ticker: str
    start_date: date
    end_date: Optional[date]

    def contains(self, day: date) -> bool:
        return self.start_date <= day and (self.end_date is None or day < self.end_date)


def normalize_ticker(ticker: str) -> str:
    """SEC/Yahoo style ``BRK-B`` -> Alpaca style ``BRK.B``."""

    return ticker.strip().upper().replace("-", ".").replace("/", ".")


def parse_intervals(text: str) -> list[MembershipInterval]:
    frame = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
    intervals: list[MembershipInterval] = []
    for row in frame.itertuples(index=False):
        ticker = normalize_ticker(str(row.ticker))
        try:
            start = date.fromisoformat(str(row.start_date).strip())
        except ValueError:
            continue
        end_raw = str(row.end_date).strip()
        end = date.fromisoformat(end_raw) if end_raw else None
        if ticker:
            intervals.append(MembershipInterval(ticker, start, end))
    return intervals


def fetch_intervals(session: Optional[requests.Session] = None) -> list[MembershipInterval]:
    http = session or requests.Session()
    response = http.get(_INTERVALS_URL, timeout=60)
    response.raise_for_status()
    intervals = parse_intervals(response.text)
    current = sum(1 for i in intervals if i.end_date is None)
    if current < 480:
        raise RuntimeError(
            f"S&P 500 membership looks broken ({current} current members); "
            "refusing to overwrite the last-known-good index flags"
        )
    return intervals


def members_on(intervals: list[MembershipInterval], day: date) -> set[str]:
    return {i.ticker for i in intervals if i.contains(day)}
