from contextlib import contextmanager

from click.testing import CliRunner

from us_picker.cli import cli


class _DummySession:
    def commit(self):
        pass


class _FakeFetcher:
    instances = []

    def __init__(self, session, console):
        self.calls = []
        self.__class__.instances.append(self)

    def _get_or_create_company(self, ticker):
        self.calls.append(("company", ticker))

    def fetch_universe(self):
        self.calls.append(("universe",))
        return {}

    def fetch_prices(self, tickers=None, days_back=1500):
        self.calls.append(("prices", tickers, days_back))
        return {}

    def fetch_all(self, tickers=None, limit=0, price_days=None):
        self.calls.append(("all", tickers, limit, price_days))
        return {}

    def fetch_recent_financials(self, tickers=None):
        self.calls.append(("recent_financials", tickers))
        return {}


@contextmanager
def _session_scope(_engine):
    yield _DummySession()


def _patch_fetch_dependencies(monkeypatch):
    _FakeFetcher.instances.clear()
    monkeypatch.setattr("us_picker.cli._get_engine_and_tables", lambda: object())
    monkeypatch.setattr("us_picker.db.connection.session_scope", _session_scope)
    monkeypatch.setattr("us_picker.data.fetcher.DataFetcher", _FakeFetcher)


def test_fetch_prices_only_accepts_price_days(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["fetch", "--prices-only", "--price-days", "15"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeFetcher.instances[-1].calls == [("prices", None, 15)]


def test_fetch_prices_only_can_refresh_universe_first(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["fetch", "--prices-only", "--refresh-universe", "--price-days", "15"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeFetcher.instances[-1].calls == [
        ("universe",),
        ("prices", None, 15),
    ]


def test_fetch_all_forwards_price_days(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["fetch", "--price-days", "15", "--limit", "3"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeFetcher.instances[-1].calls == [("all", None, 3, 15)]


def test_fetch_financials_only_calls_recent_financials(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(cli, ["fetch", "--financials-only"])

    assert result.exit_code == 0, result.output
    assert _FakeFetcher.instances[-1].calls == [("recent_financials", None)]


def test_fetch_financials_only_can_refresh_universe_first(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["fetch", "--financials-only", "--refresh-universe"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeFetcher.instances[-1].calls == [
        ("universe",),
        ("recent_financials", None),
    ]


def test_fetch_financials_only_rejects_prices_only(monkeypatch):
    _patch_fetch_dependencies(monkeypatch)

    result = CliRunner().invoke(
        cli,
        ["fetch", "--financials-only", "--prices-only"],
    )

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
