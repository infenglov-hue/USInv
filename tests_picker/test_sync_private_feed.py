import json
from pathlib import Path

from scripts_picker import sync_private_feed


def test_previous_manifest_still_resolves_after_refresh(tmp_path, monkeypatch):
    downloads = iter(["first", "second", None, "third"])

    def pull(repository: str, target: Path, *, known_asset):
        asset = next(downloads)
        if asset is None:
            assert known_asset == "second"
            return None
        target.mkdir()
        names = {"snapshot": "mobile_snapshot.db.gz", "live_tickers": "live_tickers.json"}
        for filename in names.values():
            (target / filename).write_text(asset, encoding="utf-8")
        (target / "manifest.json").write_text(
            json.dumps({key: {"filename": value, "url": value} for key, value in names.items()}),
            encoding="utf-8",
        )
        return asset

    monkeypatch.setattr(sync_private_feed, "pull_feed_bundle", pull)

    def current_snapshot():
        manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
        return tmp_path / manifest["snapshot"]["filename"]

    sync_private_feed.sync_feed("example/repo", tmp_path)
    first = current_snapshot()
    sync_private_feed.sync_feed("example/repo", tmp_path)
    second = current_snapshot()
    assert first.read_text(encoding="utf-8") == "first"
    assert sync_private_feed.sync_feed("example/repo", tmp_path) is None
    assert current_snapshot() == second
    sync_private_feed.sync_feed("example/repo", tmp_path)
    assert second.read_text(encoding="utf-8") == "second"
    assert current_snapshot().read_text(encoding="utf-8") == "third"
    assert not first.exists()
