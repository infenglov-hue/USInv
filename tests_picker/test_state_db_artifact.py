from __future__ import annotations

import gzip
import json
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from scripts_picker.state_db_artifact import (
    PART_PREFIX,
    pull_state_db,
    push_state_db,
    restore_state_db,
    store_state_db,
)


def _sqlite_like_bytes(size: int = 16_384) -> bytes:
    return b"SQLite format 3\x00" + random.Random(7).randbytes(size)


class FakeGh:
    """Stand-in for the GitHub CLI backed by a directory of release assets."""

    def __init__(self, assets_dir: Path, *, release_exists: bool = False) -> None:
        self.assets_dir = assets_dir
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.release_exists = release_exists
        self.calls: list[list[str]] = []

    @property
    def asset_names(self) -> set[str]:
        return {item.name for item in self.assets_dir.iterdir()}

    def __call__(self, args, *, token=None, check: bool = True):
        args = list(args)
        self.calls.append(args)
        returncode, stdout = 0, ""

        if args[:2] == ["release", "view"]:
            if not self.release_exists:
                returncode = 1
            elif "--json" in args:
                stdout = "\n".join(sorted(self.asset_names)) + "\n"
        elif args[:2] == ["release", "create"]:
            self.release_exists = True
        elif args[:2] == ["release", "upload"]:
            for item in args[3:]:
                if item.startswith("--"):
                    break
                shutil.copyfile(item, self.assets_dir / Path(item).name)
        elif args[:2] == ["release", "download"]:
            if not self.release_exists:
                returncode = 1
            else:
                target = Path(args[args.index("--dir") + 1])
                target.mkdir(parents=True, exist_ok=True)
                for item in self.assets_dir.iterdir():
                    shutil.copyfile(item, target / item.name)
        elif args[:2] == ["release", "delete-asset"]:
            (self.assets_dir / args[3]).unlink(missing_ok=True)
        else:  # pragma: no cover - guards against a silently ignored command
            raise AssertionError(f"unexpected gh invocation: {args}")

        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, ["gh", *args])
        return subprocess.CompletedProcess(args, returncode, stdout, "")


def test_split_state_artifact_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(_sqlite_like_bytes())
    state_dir = tmp_path / "state"

    stored = store_state_db(source, state_dir, chunk_bytes=1024)
    restored = tmp_path / "restored.db"
    result = restore_state_db(state_dir, restored)

    assert stored["part_count"] > 1
    assert not (state_dir / "us_picker.db.gz").exists()
    assert result["format"] == "split-gzip-v1"
    assert restored.read_bytes() == source.read_bytes()


def test_restore_supports_legacy_single_gzip(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    payload = _sqlite_like_bytes()
    with gzip.open(state_dir / "us_picker.db.gz", "wb") as artifact:
        artifact.write(payload)

    restored = tmp_path / "restored.db"
    result = restore_state_db(state_dir, restored)

    assert result["format"] == "legacy-gzip"
    assert restored.read_bytes() == payload


def test_restore_rejects_tampered_part(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(_sqlite_like_bytes())
    state_dir = tmp_path / "state"
    store_state_db(source, state_dir, chunk_bytes=1024)
    manifest = json.loads(
        (state_dir / "us_picker.db.gz.parts.json").read_text(encoding="utf-8")
    )
    first_part = state_dir / manifest["parts"][0]["name"]
    first_part.write_bytes(first_part.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="size mismatch"):
        restore_state_db(state_dir, tmp_path / "restored.db")


def test_push_creates_release_and_pull_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(_sqlite_like_bytes())
    gh = FakeGh(tmp_path / "release")

    pushed = push_state_db(
        source, "owner/state", chunk_bytes=1024, token="t", runner=gh
    )
    restored = tmp_path / "restored.db"
    pulled = pull_state_db("owner/state", restored, token="t", runner=gh)

    assert pushed["release_created"] is True
    assert pushed["release_tag"] == "state-current"
    assert "us_picker.db.gz.parts.json" in pushed["uploaded"]
    assert gh.asset_names == set(pushed["uploaded"])
    assert pulled["format"] == "split-gzip-v1"
    assert restored.read_bytes() == source.read_bytes()


def test_push_reuses_an_existing_release(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(_sqlite_like_bytes())
    gh = FakeGh(tmp_path / "release", release_exists=True)

    pushed = push_state_db(
        source, "owner/state", chunk_bytes=1024, token="t", runner=gh
    )

    assert pushed["release_created"] is False
    assert ["release", "create"] not in [call[:2] for call in gh.calls]


def test_push_deletes_orphan_parts_from_a_larger_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(_sqlite_like_bytes())
    gh = FakeGh(tmp_path / "release", release_exists=True)
    orphan = gh.assets_dir / f"{PART_PREFIX}099"
    orphan.write_bytes(b"left over by a bigger database")

    pushed = push_state_db(
        source, "owner/state", chunk_bytes=1_000_000, token="t", runner=gh
    )

    assert pushed["deleted_orphans"] == [f"{PART_PREFIX}099"]
    assert not orphan.exists()
    assert gh.asset_names == set(pushed["uploaded"])


def test_pull_fails_loudly_when_the_release_is_missing(tmp_path: Path) -> None:
    gh = FakeGh(tmp_path / "release", release_exists=False)

    with pytest.raises(subprocess.CalledProcessError):
        pull_state_db("owner/state", tmp_path / "restored.db", token="t", runner=gh)
