"""Refresh a local PWA from the authenticated GitHub draft feed."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from scripts_picker.mobile_feed_artifact import pull_feed_bundle

_GENERATION = re.compile(r"(?:mobile_snapshot\.db\.gz|live_tickers\.json)\.[0-9a-f]{32}")


def sync_feed(repository: str, site: Path) -> str | None:
    """Publish the verified files before switching the manifest seen by the PWA."""
    site = site.resolve()
    manifest_path = site / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    with TemporaryDirectory(prefix=".feed-sync-", dir=site) as temporary:
        staging = Path(temporary)
        bundle = staging / "bundle"
        asset = pull_feed_bundle(repository, bundle, known_asset=previous.get("private_feed_asset"))
        if asset is None:
            return None
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        generation = uuid4().hex
        retained = set()
        for key in ("snapshot", "live_tickers"):
            source = bundle / manifest[key]["filename"]
            filename = f"{source.name}.{generation}"
            source.rename(site / filename)
            manifest[key].update(filename=filename, url=filename)
            retained.update((filename, previous.get(key, {}).get("filename")))
        manifest["private_feed_asset"] = asset
        updated = staging / "manifest.json"
        updated.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        updated.replace(manifest_path)

    for path in site.iterdir():
        if path.name not in retained and _GENERATION.fullmatch(path.name) and path.is_file():
            try:
                path.unlink()
            except OSError:
                # A local client may still have a snapshot open on Windows.
                continue
    return asset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="infenglov-hue/USInv")
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=0, help="Poll seconds. Zero runs once.")
    args = parser.parse_args()
    if args.interval and args.interval < 60:
        parser.error("--interval must be 0 or at least 60 seconds")
    while True:
        try:
            asset = sync_feed(args.repository, args.site)
            if asset is not None:
                print(f"Installed private US Picker feed {asset}", flush=True)
        except Exception as exc:
            if not args.interval:
                raise
            print(f"Private feed refresh failed ({type(exc).__name__})", flush=True)
        if not args.interval:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
