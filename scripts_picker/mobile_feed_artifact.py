"""Publish and download verified US Picker bundles on a private draft release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

from scripts_picker.state_db_artifact import (
    SQLITE_HEADER,
    GhRunner,
    _ensure_release,
    _gh,
    _release_asset_names,
    _require_draft_release,
    _token_from_env,
    _view_release_visibility,
)

FEED_TAG = "feed-current"
_MANIFEST = "manifest.json"
_SNAPSHOT = "mobile_snapshot.db.gz"
_TICKERS = "live_tickers.json"
_FEED_FILES = (_MANIFEST, _SNAPSHOT, _TICKERS)
_BUNDLE_NAME = re.compile(r"us-picker-feed-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}\.zip")


def _validate_bundle(directory: Path) -> None:
    manifest = json.loads((directory / _MANIFEST).read_text(encoding="utf-8"))
    for entry, filename in (("snapshot", _SNAPSHOT), ("live_tickers", _TICKERS)):
        record = manifest.get(entry) if isinstance(manifest, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("filename") != filename
            or record.get("url") not in (filename, f"./{filename}")
        ):
            raise ValueError(f"Feed manifest must reference the relative {filename} file")

    snapshot = directory / _SNAPSHOT
    snapshot_manifest = manifest["snapshot"]
    with snapshot.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (
        snapshot_manifest.get("compression") != "gzip"
        or snapshot_manifest.get("size_bytes") != snapshot.stat().st_size
        or snapshot_manifest.get("sha256") != digest
    ):
        raise ValueError("Mobile snapshot does not match the feed manifest")
    with gzip.open(snapshot, "rb") as stream:
        if stream.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError("Mobile snapshot is not a SQLite database")

    payload = json.loads((directory / _TICKERS).read_text(encoding="utf-8"))
    tickers = payload.get("tickers") if isinstance(payload, dict) else None
    ticker_manifest = manifest["live_tickers"]
    if (
        not isinstance(tickers, list)
        or payload.get("ticker_count") != len(tickers)
        or ticker_manifest.get("ticker_count") != len(tickers)
        or ticker_manifest.get("latest_price_date") != payload.get("latest_price_date")
    ):
        raise ValueError("Live ticker list does not match the feed manifest")


def push_feed_bundle(
    source_dir: Path,
    repository: str,
    *,
    token: str | None = None,
    runner: GhRunner | None = None,
) -> str:
    """Upload one immutable archive and retain the previous successful bundle."""
    generation = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:12]}"
    asset = f"us-picker-feed-{generation}.zip"
    with tempfile.TemporaryDirectory(prefix="us_picker_feed_push_") as temporary:
        staging = Path(temporary)
        for filename in _FEED_FILES:
            shutil.copyfile(source_dir / filename, staging / filename)
        _validate_bundle(staging)
        archive = staging / asset
        with ZipFile(archive, "w", compression=ZIP_STORED) as bundle:
            for filename in _FEED_FILES:
                bundle.write(staging / filename, arcname=filename)
        _ensure_release(
            repository,
            FEED_TAG,
            token=token,
            runner=runner,
            title="US Picker mobile feed (private draft)",
            notes="Verified mobile feed bundles for authenticated local use. Keep this release in draft.",
        )
        _gh(
            ["release", "upload", FEED_TAG, str(archive), "--repo", repository],
            token=token,
            runner=runner,
        )

    uploaded = _release_asset_names(repository, FEED_TAG, token=token, runner=runner, check=False)
    bundles = [name for name in uploaded if _BUNDLE_NAME.fullmatch(name)]
    for old_bundle in bundles[:-2]:
        _gh(
            ["release", "delete-asset", FEED_TAG, old_bundle, "--repo", repository, "--yes"],
            token=token,
            runner=runner,
            check=False,
        )
    return asset


def pull_feed_bundle(
    repository: str,
    target_dir: Path,
    *,
    known_asset: str | None = None,
    token: str | None = None,
    runner: GhRunner | None = None,
) -> str | None:
    """Atomically install the latest changed bundle in a new local directory."""
    _require_draft_release(
        _view_release_visibility(repository, FEED_TAG, token=token, runner=runner)
    )
    uploaded = _release_asset_names(repository, FEED_TAG, token=token, runner=runner)
    asset = next((name for name in reversed(uploaded) if _BUNDLE_NAME.fullmatch(name)), None)
    if asset is None:
        raise FileNotFoundError(
            "No completed US Picker feed bundle is available on the private draft"
        )
    if asset == known_asset:
        return None
    if target_dir.exists():
        raise FileExistsError(f"Mobile feed destination already exists: {target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".us_picker_feed_", dir=target_dir.parent) as temporary:
        staging = Path(temporary)
        _gh(
            [
                "release",
                "download",
                FEED_TAG,
                "--repo",
                repository,
                "--pattern",
                asset,
                "--dir",
                temporary,
            ],
            token=token,
            runner=runner,
        )
        verified = staging / "verified"
        verified.mkdir()
        with ZipFile(staging / asset) as bundle:
            if sorted(bundle.namelist()) != sorted(_FEED_FILES):
                raise ValueError("Unexpected contents in mobile feed bundle")
            for filename in _FEED_FILES:
                with (
                    bundle.open(filename) as source,
                    (verified / filename).open("wb") as destination,
                ):
                    shutil.copyfileobj(source, destination)
        _validate_bundle(verified)
        verified.rename(target_dir)
    return asset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    push = commands.add_parser("push")
    push.add_argument("--repository", required=True)
    push.add_argument("--source-dir", type=Path, required=True)
    pull = commands.add_parser("pull")
    pull.add_argument("--repository", required=True)
    pull.add_argument("--target-dir", type=Path, required=True)
    pull.add_argument("--known-asset")
    args = parser.parse_args()
    if args.command == "push":
        asset = push_feed_bundle(args.source_dir, args.repository, token=_token_from_env())
        print(json.dumps({"asset": asset}))
    else:
        asset = pull_feed_bundle(
            args.repository, args.target_dir, known_asset=args.known_asset, token=_token_from_env()
        )
        print(json.dumps({"asset": asset, "target_dir": str(args.target_dir) if asset else None}))


if __name__ == "__main__":
    main()
