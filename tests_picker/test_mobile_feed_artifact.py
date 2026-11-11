from __future__ import annotations

from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from zipfile import BadZipFile

import pytest

from scripts_picker.mobile_feed_artifact import FEED_TAG, pull_feed_bundle, push_feed_bundle


def _feed(directory: Path, *, price: int = 100, base: str = "") -> None:
    directory.mkdir()
    db = directory / "temporary.db"
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("CREATE TABLE close (price INTEGER)")
        connection.execute("INSERT INTO close VALUES (?)", (price,))
        connection.commit()
    snapshot = gzip.compress(db.read_bytes())
    db.unlink()
    (directory / "mobile_snapshot.db.gz").write_bytes(snapshot)
    (directory / "live_tickers.json").write_text(
        json.dumps({"ticker_count": 1, "tickers": ["SPY"], "latest_price_date": "2026-11-10"}),
        encoding="utf-8",
    )
    manifest = {
        "snapshot": {
            "filename": "mobile_snapshot.db.gz",
            "url": base + "mobile_snapshot.db.gz",
            "compression": "gzip",
            "size_bytes": len(snapshot),
            "sha256": hashlib.sha256(snapshot).hexdigest(),
        },
        "live_tickers": {
            "filename": "live_tickers.json",
            "url": base + "live_tickers.json",
            "ticker_count": 1,
            "latest_price_date": "2026-11-10",
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class FakeDraft:
    def __init__(self, *, draft: bool | None = None):
        self.draft = draft
        self.assets: dict[str, bytes] = {}
        self.fail_upload = False
        self.downloads = 0

    def __call__(self, args, *, token=None, check=True):
        assert args[2] == FEED_TAG
        command = args[1]
        code, stdout, stderr = 0, "", ""
        if command == "view":
            if self.draft is None:
                code, stderr = 1, "release not found"
            elif "isDraft" in args:
                stdout = "true" if self.draft else "false"
            else:
                stdout = "\n".join(self.assets)
        elif command == "create":
            assert "--draft" in args
            self.draft = True
        elif command == "upload":
            assert "--clobber" not in args
            archive = Path(args[3])
            if self.fail_upload:
                code = 1
            else:
                self.assets[archive.name] = archive.read_bytes()
        elif command == "download":
            self.downloads += 1
            asset = args[args.index("--pattern") + 1]
            target = Path(args[args.index("--dir") + 1])
            (target / asset).write_bytes(self.assets[asset])
        elif command == "delete-asset":
            del self.assets[args[3]]
        else:
            raise AssertionError(args)
        result = subprocess.CompletedProcess(args, code, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def test_failed_upload_keeps_previous_bundle_and_polls_without_redownload(tmp_path):
    previous = tmp_path / "previous"
    _feed(previous)
    newer = tmp_path / "newer"
    _feed(newer, price=101)
    draft = FakeDraft()
    asset = push_feed_bundle(previous, "owner/project", runner=draft)
    draft.fail_upload = True
    with pytest.raises(subprocess.CalledProcessError):
        push_feed_bundle(newer, "owner/project", runner=draft)
    assert list(draft.assets) == [asset]

    restored = tmp_path / "restored"
    assert pull_feed_bundle("owner/project", restored, runner=draft) == asset
    assert (restored / "mobile_snapshot.db.gz").read_bytes() == (
        previous / "mobile_snapshot.db.gz"
    ).read_bytes()
    unchanged = tmp_path / "unchanged"
    assert pull_feed_bundle("owner/project", unchanged, known_asset=asset, runner=draft) is None
    assert draft.downloads == 1 and not unchanged.exists()


def test_retains_two_good_bundles_and_corrupt_download_never_installs(tmp_path):
    source = tmp_path / "source"
    _feed(source)
    draft = FakeDraft()
    pushes = [push_feed_bundle(source, "owner/project", runner=draft) for _ in range(3)]
    assert list(draft.assets) == pushes[-2:]
    draft.assets[pushes[-1]] = b"corrupted archive"

    target = tmp_path / "invalid"
    with pytest.raises(BadZipFile):
        pull_feed_bundle("owner/project", target, runner=draft)
    assert not target.exists()


def test_public_draft_or_absolute_source_is_rejected(tmp_path):
    source = tmp_path / "relative"
    _feed(source)
    draft = FakeDraft(draft=False)
    with pytest.raises(ValueError, match="private GitHub draft"):
        push_feed_bundle(source, "owner/project", runner=draft)
    with pytest.raises(ValueError, match="private GitHub draft"):
        pull_feed_bundle("owner/project", tmp_path / "invalid", runner=draft)
    assert not draft.assets and draft.downloads == 0

    absolute = tmp_path / "absolute"
    _feed(absolute, base="https://example.com/")
    with pytest.raises(ValueError, match="relative mobile_snapshot"):
        push_feed_bundle(absolute, "owner/project", runner=FakeDraft())
