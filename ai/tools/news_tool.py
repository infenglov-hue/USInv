"""News and catalyst intelligence tool for funds and ETFs."""

from ai.core.models import NewsItem, SentimentType
from ai.tools.alpaca_client import AlpacaClient


class NewsTool:
    """Collects news articles and extracts catalysts for candidate funds and ETFs."""

    # Map leveraged / thematic ETFs to high-impact underlying drivers
    PROXY_MAPPINGS = {
        "TQQQ": ["QQQ", "NVDA", "AAPL", "MSFT"],
        "SOXL": ["SMH", "NVDA", "TSM", "AMD", "AVGO"],
        "TECL": ["XLK", "MSFT", "AAPL", "NVDA"],
        "FNGU": ["META", "AMZN", "GOOGL", "NFLX"],
        "NVDL": ["NVDA"],
        "UPRO": ["SPY"],
        "TNA": ["IWM"],
        "ARKK": ["TSLA", "COIN", "ROKU"],
        "IBIT": ["BITO", "COIN"],
        "WGMI": ["COIN", "MARA", "RIOT"],
        "XBI": ["IBB"],
        "SQQQ": ["QQQ"],
        "SOXS": ["SMH", "NVDA"],
        "UCO": ["USO"],
        "BOIL": ["UNG"],
        "UVXY": ["VXX"],
    }

    def __init__(self, client: AlpacaClient):
        self.client = client

    def get_catalysts_for_symbol(
        self,
        symbol: str,
        limit: int = 8,
    ) -> tuple[list[NewsItem], list[str], list[str]]:
        """Fetch news and extract bull catalysts and bear risks for a symbol."""
        sym_clean = symbol.upper()
        query_syms = [sym_clean]
        if sym_clean in self.PROXY_MAPPINGS:
            query_syms.extend(self.PROXY_MAPPINGS[sym_clean])

        news_items = self.client.get_news(symbols=query_syms, limit=limit)

        bull_catalysts: list[str] = []
        bear_risks: list[str] = []

        for item in news_items:
            headline = item.headline.strip()
            if not headline:
                continue

            if item.sentiment == SentimentType.BULLISH:
                if len(bull_catalysts) < 3:
                    bull_catalysts.append(f"{headline} ({item.source})")
            elif item.sentiment == SentimentType.BEARISH:
                if len(bear_risks) < 3:
                    bear_risks.append(f"{headline} ({item.source})")

        # Sensible defaults if news items lack polarity
        if not bull_catalysts:
            bull_catalysts.append(f"Positive momentum and sustained institutional flows into {sym_clean} ecosystem.")
        if not bear_risks:
            bear_risks.append(f"Elevated beta volatility and macro sensitivity to sudden rate / yield shifts.")

        return news_items, bull_catalysts, bear_risks
