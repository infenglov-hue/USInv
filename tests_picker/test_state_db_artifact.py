from __future__ import annotations

from contextlib import closing
from fnmatch import fnmatchcase
import gzip
import json
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess

import pytest

from scripts_picker.state_db_artifact import (
    PART_PREFIX,
    pull_state_db,
    push_state_db,
    restore_state_db,
    store_state_db,
)


def _make_database(path: Path, seed: int = 7) -> bytes:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE payload (content BLOB NOT NULL)")
        connection.execute("INSERT INTO payload VALUES (?)", (random.Random(seed).randbytes(16_384),))
        connection.commit()
    return path.read_bytes()


class FakeGh:
    """Stand-in for the GitHub CLI backed by a directory of release assets."""

    def __init__(
        self, assets_dir: Path, *, release_exists: bool = False, draft: bool = True
    ) -> None:
        self.assets_dir = assets_dir
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.release_exists = release_exists
        self.draft = draft
        self.calls: list[list[str]] = []
        self.upload_order: list[str] = []
        self.fail_next_manifest = False

    @property
    def asset_names(self) -> set[str]:
        return {item.name for item in self.assets_dir.iterdir()}

    def __call__(self, args, *, token=None, check: bool = True):
        args = list(args)
        self.calls.append(args)
        returncode, stdout, stderr = 0, "", ""

        if args[:2] == ["release", "view"]:
            if not self.release_exists:
                returncode = 1
                stderr = "release not found\n"
            elif "isDraft" in args:
                stdout = "true\n" if self.draft else "false\n"
            elif "assets" in args:
                names = sorted(self.asset_names.difference(self.upload_order))
                names.extend(name for name in self.upload_order if name in self.asset_names)
                stdout = "\n".join(names) + "\n"
        elif args[:2] == ["release", "create"]:
            self.release_exists = True
            self.draft = "--draft" in args
        elif args[:2] == ["release", "upload"]:
            assert "--clobber" not in args
            for item in args[3:]:
                if item.startswith("--"):
                    break
                name = Path(item).name
                if self.fail_next_manifest and name.endswith(".json"):
                    self.fail_next_manifest = False
                    returncode = 1
                    break
                assert name not in self.asset_names
                shutil.copyfile(item, self.assets_dir / name)
                self.upload_order.append(name)
        elif args[:2] == ["release", "download"]:
            if not self.release_exists:
                returncode = 1
            else:
                target = Path(args[args.index("--dir") + 1])
                target.mkdir(parents=True, exist_ok=True)
                patterns = [args[i + 1] for i, value in enumerate(args) if value == "--pattern"]
                for item in self.assets_dir.iterdir():
                    if any(fnmatchcase(item.name, pattern) for pattern in patterns):
                        shutil.copyfile(item, target / item.name)
        elif args[:2] == ["release", "delete-asset"]:
            (self.assets_dir / args[3]).unlink(missing_ok=True)
        else:  # pragma: no cover - guards against a silently ignored command
            raise AssertionError(f"unexpected gh invocation: {args}")

        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, ["gh", *args])
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_split_state_artifact_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_database(source)
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
    payload = _make_database(tmp_path / "source.db")
    with gzip.open(state_dir / "us_picker.db.gz", "wb") as artifact:
        artifact.write(payload)

    restored = tmp_path / "restored.db"
    result = restore_state_db(state_dir, restored)

    assert result["format"] == "legacy-gzip"
    assert restored.read_bytes() == payload


def test_restore_rejects_tampered_part(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_database(source)
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
    _make_database(source)
    gh = FakeGh(tmp_path / "release")

    pushed = push_state_db(
        source, "owner/state", chunk_bytes=1024, token="t", runner=gh
    )
    restored = tmp_path / "restored.db"
    pulled = pull_state_db("owner/state", restored, token="t", runner=gh)

    assert pushed["release_created"] is True
    assert gh.draft
    assert pushed["release_tag"] == "state-current"
    assert pushed["uploaded"][-1].startswith("us_picker.db.gz.parts.")
    assert pushed["uploaded"][-1].endswith(".json")
    assert gh.upload_order == pushed["uploaded"]
    assert gh.asset_names == set(pushed["uploaded"])
    assert pulled["format"] == "split-gzip-v1"
    assert restored.read_bytes() == source.read_bytes()


def test_push_reuses_an_existing_release(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_database(source)
    gh = FakeGh(tmp_path / "release", release_exists=True)

    pushed = push_state_db(
        source, "owner/state", chunk_bytes=1024, token="t", runner=gh
    )

    assert pushed["release_created"] is False
    assert ["release", "create"] not in [call[:2] for call in gh.calls]


def test_push_keeps_two_completed_generations_and_legacy_state(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_database(source)
    gh = FakeGh(tmp_path / "release", release_exists=True)
    legacy = store_state_db(source, gh.assets_dir, chunk_bytes=1_000_000)
    legacy_assets = gh.asset_names.copy()
    restored = tmp_path / "restored.db"
    pull_state_db("owner/state", restored, token="t", runner=gh)
    assert restored.read_bytes() == source.read_bytes()
    assert f"{PART_PREFIX}000" in legacy["part_names"]

    first = push_state_db(
        source, "owner/state", chunk_bytes=1_000_000, token="t", runner=gh
    )
    second = push_state_db(source, "owner/state", chunk_bytes=1_000_000, token="t", runner=gh)
    latest = push_state_db(source, "owner/state", chunk_bytes=1_000_000, token="t", runner=gh)
    assert set(latest["deleted_orphans"]) == set(first["uploaded"])
    assert gh.asset_names == legacy_assets.union(second["uploaded"], latest["uploaded"])


def test_failed_generation_manifest_upload_leaves_prior_state_restorable(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    previous_bytes = _make_database(source)
    gh = FakeGh(tmp_path / "release")
    first = push_state_db(source, "owner/state", chunk_bytes=1024, token="t", runner=gh)
    newer = tmp_path / "newer.db"
    _make_database(newer, seed=42)
    gh.fail_next_manifest = True

    with pytest.raises(subprocess.CalledProcessError):
        push_state_db(newer, "owner/state", chunk_bytes=1024, token="t", runner=gh)

    assert gh.asset_names > set(first["uploaded"])
    restored = tmp_path / "restored.db"
    pull_state_db("owner/state", restored, token="t", runner=gh)
    assert restored.read_bytes() == previous_bytes
    download = next(call for call in reversed(gh.calls) if call[:2] == ["release", "download"])
    assert first["uploaded"][-1] in download
    assert len([value for value in download if value == "--pattern"]) == 2


def test_pull_fails_loudly_when_the_release_is_missing(tmp_path: Path) -> None:
    gh = FakeGh(tmp_path / "release", release_exists=False)

    with pytest.raises(subprocess.CalledProcessError):
        pull_state_db("owner/state", tmp_path / "restored.db", token="t", runner=gh)


def test_public_release_is_rejected_before_upload_or_download(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_database(source)
    gh = FakeGh(tmp_path / "release", release_exists=True, draft=False)

    with pytest.raises(ValueError, match="private GitHub draft"):
        push_state_db(source, "owner/state", token="t", runner=gh)
    with pytest.raises(ValueError, match="private GitHub draft"):
        pull_state_db("owner/state", tmp_path / "restored.db", token="t", runner=gh)
    assert [call[:2] for call in gh.calls] == [["release", "view"], ["release", "view"]]


def test_restore_rejects_corrupt_sqlite_without_replacing_target(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    before = _make_database(target)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with gzip.open(state_dir / "us_picker.db.gz", "wb") as artifact:
        artifact.write(before[:500])

    with pytest.raises(ValueError, match="SQLite quick_check failed"):
        restore_state_db(state_dir, target)
    assert target.read_bytes() == before
