"""FRED client for the US macro inputs (TCMB's role in BIST Picker).

Series used:

* ``DFF``          effective federal funds rate (policy rate), daily, %
* ``DGS10``        10-year Treasury constant-maturity yield, daily, %
* ``CPIAUCSL``     CPI-U index, monthly (seasonally adjusted)
* ``T5YIFR``       5y5y forward breakeven inflation (DCF terminal inflation), %
* ``BAA10Y``       Moody's Baa corporate yield minus 10y Treasury, daily, %
  (credit-stress leg; the analogue of Turkey's 5y CDS in the BIST cash
  signal).  ``BAMLH0A0HYM2`` (HY OAS) is only published for ~3 years.
* ``VIXCLS``       CBOE VIX close, daily

Uses the public ``fredgraph.csv`` download, which needs no API key.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import date
from typing import Optional

import pandas as pd
import requests

from us_picker.utils.rate_limiter import RateLimiter

logger = logging.getLogger("us_picker.data.sources.fred")

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

POLICY_RATE = "DFF"
TEN_YEAR = "DGS10"
CPI = "CPIAUCSL"
LONG_RUN_INFLATION = "T5YIFR"
HIGH_YIELD_OAS = "BAMLH0A0HYM2"  # FRED keeps only ~3 years (ICE licensing)
# Moody's Baa corporate minus 10y Treasury: continuous daily history since
# 1986, so the credit-stress leg exists for the whole backtest window.
CREDIT_SPREAD = "BAA10Y"
VIX = "VIXCLS"


def parse_fredgraph_csv(text: str) -> pd.Series:
    """Parse a fredgraph CSV (``observation_date,<ID>``) into a float Series."""

    frame = pd.read_csv(io.StringIO(text))
    if frame.shape[1] < 2:
        return pd.Series(dtype=float)
    dates = pd.to_datetime(frame.iloc[:, 0], errors="coerce")
    values = pd.to_numeric(frame.iloc[:, 1], errors="coerce")
    series = pd.Series(values.values, index=dates.dt.date).dropna()
    series = series[~pd.isna(series.index)]
    return series.astype(float).sort_index()


class FREDClient:
    def __init__(
        self,
        rate_limiter: Optional[RateLimiter] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter(min_delay=0.6, name="fred")
        self._session = session or requests.Session()

    def fetch_series(self, series_id: str, start: date, end: Optional[date] = None) -> pd.Series:
        """Observations as a float Series indexed by ``date`` (missing dropped)."""

        params = {"id": series_id, "cosd": start.isoformat()}
        if end is not None:
            params["coed"] = end.isoformat()
        delay = 2.0
        for attempt in range(1, 6):
            self._rate_limiter.wait()
            try:
                response = self._session.get(_CSV_URL, params=params, timeout=60)
            except requests.RequestException as exc:
                logger.warning("FRED %s failed (%s): %s", series_id, attempt, exc)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            series = parse_fredgraph_csv(response.text)
            if end is not None:
                series = series[series.index <= end]
            return series[series.index >= start]
        raise RuntimeError(f"FRED series {series_id} kept failing")
