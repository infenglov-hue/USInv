"""Alpaca REST client for market data, assets, and news."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests

from ai.core.models import BarData, NewsItem, SentimentType


class AlpacaClient:
    """Authenticated client for Alpaca Market Data and Trading APIs."""

    def __init__(
        self,
        key_id: str | None = None,
        secret_key: str | None = None,
        base_url: str = "https://paper-api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        timeout: int = 15,
    ):
        self.key_id = key_id or os.getenv("ALPACA_KEY_ID")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")

        if not self.key_id or not self.secret_key:
            self._load_env_fallback()

        self.base_url = base_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.feed = feed
        self.timeout = timeout

    def _load_env_fallback(self) -> None:
        """Attempt to read .env file directly from project root if not in os.environ."""
        candidate_paths = [
            Path.cwd() / ".env",
            Path.cwd().parent / ".env",
            Path(__file__).resolve().parent.parent.parent / ".env",
        ]
        for path in candidate_paths:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k == "ALPACA_KEY_ID" and not self.key_id:
                                self.key_id = v
                            elif k == "ALPACA_SECRET_KEY" and not self.secret_key:
                                self.secret_key = v
                if self.key_id and self.secret_key:
                    break

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
            "Accept": "application/json",
        }

    def get_bars(
        self,
        symbols: list[str] | str,
        timeframe: str = "1Day",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = 200,
    ) -> dict[str, list[BarData]]:
        """Fetch historical bars for one or more symbols."""
        if isinstance(symbols, str):
            symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            symbol_list = [s.upper() for s in symbols]

        if not symbol_list:
            return {}

        if start is None:
            # Default to 120 days ago
            start = datetime.now(timezone.utc) - timedelta(days=150)

        start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")

        url = f"{self.data_url}/v2/stocks/bars"
        params: dict[str, Any] = {
            "symbols": ",".join(symbol_list),
            "timeframe": timeframe,
            "start": start_str,
            "feed": self.feed,
        }
        if end is not None:
            params["end"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        if limit is not None:
            # Alpaca limit is global across all symbols; scale accordingly or omit
            params["limit"] = min(10000, limit * len(symbol_list))

        result: dict[str, list[BarData]] = {s: [] for s in symbol_list}
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                return result

            data = resp.json().get("bars", {})
            for sym, bar_list in data.items():
                if not bar_list:
                    continue
                parsed_bars = []
                for b in bar_list:
                    # ISO timestamp parsing
                    ts_str = b["t"]
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    parsed_bars.append(
                        BarData(
                            timestamp=ts,
                            open=float(b["o"]),
                            high=float(b["h"]),
                            low=float(b["l"]),
                            close=float(b["c"]),
                            volume=float(b["v"]),
                            vwap=float(b.get("vw", b["c"])),
                        )
                    )
                result[sym] = parsed_bars
        except Exception:
            return result

        return result

    def get_asset(self, symbol: str) -> dict[str, Any] | None:
        """Fetch asset metadata including tradability and margin requirements."""
        url = f"{self.base_url}/v2/assets/{symbol.upper()}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def get_news(
        self,
        symbols: list[str] | str | None = None,
        limit: int = 10,
    ) -> list[NewsItem]:
        """Fetch financial news articles and extract sentiment."""
        url = f"{self.data_url}/v1beta1/news"
        params: dict[str, Any] = {
            "limit": limit,
            "sort": "desc",
        }
        if symbols:
            if isinstance(symbols, str):
                params["symbols"] = symbols.upper()
            else:
                params["symbols"] = ",".join(s.upper() for s in symbols)

        items: list[NewsItem] = []
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                return items

            raw_news = resp.json().get("news", [])
            for n in raw_news:
                ts_str = n.get("created_at") or n.get("updated_at")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    ts = datetime.now(timezone.utc)

                headline = n.get("headline", "")
                summary = n.get("summary", "")

                # Basic heuristic sentiment detection
                text = (headline + " " + summary).lower()
                pos_words = ["surge", "rally", "record", "jump", "bullish", "beat", "breakout", "soar", "gain", "upgrade"]
                neg_words = ["crash", "drop", "plunge", "bearish", "miss", "recession", "slump", "fall", "warning", "downgrade"]

                pos_score = sum(1 for w in pos_words if w in text)
                neg_score = sum(1 for w in neg_words if w in text)

                if pos_score > neg_score:
                    sentiment = SentimentType.BULLISH
                elif neg_score > pos_score:
                    sentiment = SentimentType.BEARISH
                else:
                    sentiment = SentimentType.NEUTRAL

                items.append(
                    NewsItem(
                        id=str(n.get("id", "")),
                        headline=headline,
                        summary=summary,
                        source=n.get("source", "AlpacaNews"),
                        published_at=ts,
                        symbols=n.get("symbols", []),
                        sentiment=sentiment,
                        relevance_score=round(min(1.0, 0.5 + (pos_score + neg_score) * 0.1), 2),
                    )
                )
        except Exception:
            return items

        return items
