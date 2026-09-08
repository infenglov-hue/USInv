"""Technical analysis and quantitative profiling tool for individual ETFs."""

from ai.core.metrics import calculate_quant_metrics
from ai.core.models import BarData, QuantMetrics
from ai.tools.alpaca_client import AlpacaClient


class TechnicalTool:
    """Tool to calculate technical momentum, volatility, and trend indicators."""

    def __init__(self, client: AlpacaClient):
        self.client = client
        self._cached_benchmarks: dict[str, list[BarData]] = {}

    def get_benchmark_bars(self, force_refresh: bool = False) -> tuple[list[BarData], list[BarData]]:
        """Fetch SPY and QQQ bars for beta estimation."""
        if not force_refresh and "SPY" in self._cached_benchmarks and "QQQ" in self._cached_benchmarks:
            return self._cached_benchmarks["SPY"], self._cached_benchmarks["QQQ"]

        bars = self.client.get_bars(["SPY", "QQQ"], limit=200)
        self._cached_benchmarks["SPY"] = bars.get("SPY", [])
        self._cached_benchmarks["QQQ"] = bars.get("QQQ", [])
        return self._cached_benchmarks["SPY"], self._cached_benchmarks["QQQ"]

    def analyze_symbol(self, symbol: str, custom_bars: list[BarData] | None = None) -> tuple[QuantMetrics, list[BarData]]:
        """Analyze a symbol and return its quant metrics and historical bars."""
        spy_bars, qqq_bars = self.get_benchmark_bars()

        if custom_bars is not None:
            bars = custom_bars
        else:
            bars_dict = self.client.get_bars(symbol, limit=200)
            bars = bars_dict.get(symbol.upper(), [])

        metrics = calculate_quant_metrics(
            symbol=symbol.upper(),
            bars=bars,
            benchmark_spy_bars=spy_bars,
            benchmark_qqq_bars=qqq_bars,
        )
        return metrics, bars
