"""Store and restore the private runtime SQLite DB as verified gzip shards.

``store``/``restore`` move the artifact between a SQLite file and a directory
of verified shards. ``push``/``pull`` wrap those with the transport: the
shards live as assets on one rolling GitHub Release in the private state
repo, not as git commits.

Why a release and not a commit: the pipeline rewrites the whole ~100 MB
artifact 2-3x a day, and git keeps every copy forever. By 2026-07-27 that was
10.6 GB of history no job ever read, while a full clone left the runner with
67 MB of free disk. Release assets sit outside the git object store, so the
repo stops growing and clones stay small. The shard format is unchanged, so
an artifact pushed to a release restores with exactly the same size/sha
verification as a git-tracked one.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable, Optional, Sequence


LEGACY_NAME = "us_picker.db.gz"
MANIFEST_NAME = "us_picker.db.gz.parts.json"
PART_PREFIX = "us_picker.db.gz.part"
DEFAULT_CHUNK_BYTES = 32 * 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"

RELEASE_TAG = "state-current"
RELEASE_TITLE = "Current runtime state"
RELEASE_NOTES = (
    "Rolling runtime database artifact, replaced in place by the pipeline.\n\n"
    "Restore with `python scripts_picker/state_db_artifact.py pull --repository "
    "<owner>/<repo> --target data/us_picker.db`. The shard manifest in "
    "`us_picker.db.gz.parts.json` is authoritative: restore reads only the "
    "parts it names, so an orphan asset left by a larger earlier artifact is "
    "ignored rather than silently mixed in."
)

GhRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_sqlite_header(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise ValueError(f"Restored artifact is not SQLite: {path}")


def restore_state_db(state_dir: Path, target: Path) -> dict[str, object]:
    """Restore either the verified shard format or the legacy single gzip."""

    state_dir = state_dir.resolve()
    target = target.resolve()
    manifest_path = state_dir / MANIFEST_NAME
    legacy_path = state_dir / LEGACY_NAME
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bist_state_restore_") as temp_dir:
        compressed_path = Path(temp_dir) / LEGACY_NAME
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "split-gzip-v1":
                raise ValueError(f"Unsupported state artifact format: {manifest_path}")
            expected_parts = manifest.get("parts")
            if not isinstance(expected_parts, list) or not expected_parts:
                raise ValueError(f"State artifact has no parts: {manifest_path}")
            with compressed_path.open("wb") as combined:
                for index, item in enumerate(expected_parts):
                    expected_name = f"{PART_PREFIX}{index:03d}"
                    if not isinstance(item, dict) or item.get("name") != expected_name:
                        raise ValueError(f"Invalid state part order at index {index}")
                    part_path = state_dir / expected_name
                    if not part_path.is_file():
                        raise FileNotFoundError(f"Missing state artifact part: {part_path}")
                    if part_path.stat().st_size != int(item.get("size", -1)):
                        raise ValueError(f"State part size mismatch: {part_path}")
                    if _sha256(part_path) != item.get("sha256"):
                        raise ValueError(f"State part checksum mismatch: {part_path}")
                    with part_path.open("rb") as part:
                        shutil.copyfileobj(part, combined, 1024 * 1024)
            if compressed_path.stat().st_size != int(
                manifest.get("compressed_size", -1)
            ):
                raise ValueError("Combined state artifact size mismatch")
            if _sha256(compressed_path) != manifest.get("compressed_sha256"):
                raise ValueError("Combined state artifact checksum mismatch")
            artifact_format = "split-gzip-v1"
        elif legacy_path.exists():
            shutil.copyfile(legacy_path, compressed_path)
            artifact_format = "legacy-gzip"
        else:
            raise FileNotFoundError(
                f"Missing {MANIFEST_NAME} and legacy {LEGACY_NAME} in {state_dir}"
            )

        temporary_target = target.with_name(f".{target.name}.restore.tmp")
        try:
            with gzip.open(compressed_path, "rb") as source, temporary_target.open(
                "wb"
            ) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            _validate_sqlite_header(temporary_target)
            temporary_target.replace(target)
        finally:
            temporary_target.unlink(missing_ok=True)

    return {
        "format": artifact_format,
        "target": str(target),
        "size": target.stat().st_size,
    }


def store_state_db(
    source: Path,
    state_dir: Path,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict[str, object]:
    """Gzip a SQLite DB and split it into GitHub-safe verified parts."""

    source = source.resolve()
    state_dir = state_dir.resolve()
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    _validate_sqlite_header(source)
    state_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bist_state_store_") as temp_dir:
        temp_root = Path(temp_dir)
        compressed_path = temp_root / LEGACY_NAME
        with source.open("rb") as input_file, gzip.open(
            compressed_path, "wb", compresslevel=9
        ) as output_file:
            shutil.copyfileobj(input_file, output_file, 1024 * 1024)

        parts: list[dict[str, object]] = []
        with compressed_path.open("rb") as compressed:
            index = 0
            while block := compressed.read(chunk_bytes):
                name = f"{PART_PREFIX}{index:03d}"
                part_path = temp_root / name
                part_path.write_bytes(block)
                parts.append(
                    {
                        "name": name,
                        "size": len(block),
                        "sha256": hashlib.sha256(block).hexdigest(),
                    }
                )
                index += 1

        manifest = {
            "format": "split-gzip-v1",
            "source_size": source.stat().st_size,
            "compressed_size": compressed_path.stat().st_size,
            "compressed_sha256": _sha256(compressed_path),
            "chunk_bytes": chunk_bytes,
            "parts": parts,
        }

        for stale_part in state_dir.glob(f"{PART_PREFIX}*"):
            stale_part.unlink()
        (state_dir / LEGACY_NAME).unlink(missing_ok=True)
        (state_dir / MANIFEST_NAME).unlink(missing_ok=True)
        for item in parts:
            name = str(item["name"])
            shutil.move(str(temp_root / name), state_dir / name)
        (state_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    return {
        "format": "split-gzip-v1",
        "source": str(source),
        "source_size": source.stat().st_size,
        "compressed_size": manifest["compressed_size"],
        "part_count": len(parts),
        "part_names": [item["name"] for item in parts],
    }


def _run_gh(
    args: Sequence[str],
    *,
    token: Optional[str] = None,
    check: bool = True,
) -> "subprocess.CompletedProcess[str]":
    """Invoke the GitHub CLI, passing the token through the environment."""

    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    return subprocess.run(
        ["gh", *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _gh(
    args: Sequence[str],
    *,
    token: Optional[str],
    runner: Optional[GhRunner],
    check: bool = True,
) -> "subprocess.CompletedProcess[str]":
    return (runner or _run_gh)(args, token=token, check=check)


def _ensure_release(
    repository: str,
    tag: str,
    *,
    token: Optional[str],
    runner: Optional[GhRunner],
) -> bool:
    """Create the rolling release when it does not exist yet."""

    view = _gh(
        ["release", "view", tag, "--repo", repository],
        token=token,
        runner=runner,
        check=False,
    )
    if view.returncode == 0:
        return False
    _gh(
        [
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--title",
            RELEASE_TITLE,
            "--notes",
            RELEASE_NOTES,
        ],
        token=token,
        runner=runner,
    )
    return True


def _release_asset_names(
    repository: str,
    tag: str,
    *,
    token: Optional[str],
    runner: Optional[GhRunner],
) -> list[str]:
    listed = _gh(
        [
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "assets",
            "--jq",
            ".assets[].name",
        ],
        token=token,
        runner=runner,
        check=False,
    )
    if listed.returncode != 0:
        return []
    return [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()]


def pull_state_db(
    repository: str,
    target: Path,
    *,
    tag: str = RELEASE_TAG,
    token: Optional[str] = None,
    runner: Optional[GhRunner] = None,
) -> dict[str, object]:
    """Download the release assets and restore them into *target*."""

    with tempfile.TemporaryDirectory(prefix="bist_state_pull_") as temp_dir:
        state_dir = Path(temp_dir)
        _gh(
            [
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--dir",
                str(state_dir),
                "--clobber",
            ],
            token=token,
            runner=runner,
        )
        result = restore_state_db(state_dir, target)

    result["repository"] = repository
    result["release_tag"] = tag
    return result


def push_state_db(
    source: Path,
    repository: str,
    *,
    tag: str = RELEASE_TAG,
    token: Optional[str] = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    runner: Optional[GhRunner] = None,
) -> dict[str, object]:
    """Shard *source* and upload it over the rolling release assets."""

    with tempfile.TemporaryDirectory(prefix="bist_state_push_") as temp_dir:
        state_dir = Path(temp_dir)
        result = store_state_db(source, state_dir, chunk_bytes=chunk_bytes)
        created = _ensure_release(repository, tag, token=token, runner=runner)

        assets = sorted(item for item in state_dir.iterdir() if item.is_file())
        _gh(
            [
                "release",
                "upload",
                tag,
                *[str(item) for item in assets],
                "--repo",
                repository,
                "--clobber",
            ],
            token=token,
            runner=runner,
        )
        uploaded = [item.name for item in assets]

        # A smaller artifact leaves higher-numbered parts behind. restore
        # ignores them (the manifest names what it reads), but an orphan
        # part is confusing to anyone inspecting the release by hand.
        orphans = [
            name
            for name in _release_asset_names(
                repository, tag, token=token, runner=runner
            )
            if name.startswith(PART_PREFIX) and name not in uploaded
        ]
        for name in orphans:
            _gh(
                ["release", "delete-asset", tag, name, "--repo", repository, "--yes"],
                token=token,
                runner=runner,
                check=False,
            )

    result["repository"] = repository
    result["release_tag"] = tag
    result["release_created"] = created
    result["uploaded"] = uploaded
    result["deleted_orphans"] = orphans
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--state-dir", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)

    store = subparsers.add_parser("store")
    store.add_argument("--source", type=Path, required=True)
    store.add_argument("--state-dir", type=Path, required=True)
    store.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)

    pull = subparsers.add_parser("pull")
    pull.add_argument("--repository", required=True)
    pull.add_argument("--target", type=Path, required=True)
    pull.add_argument("--tag", default=RELEASE_TAG)

    push = subparsers.add_parser("push")
    push.add_argument("--source", type=Path, required=True)
    push.add_argument("--repository", required=True)
    push.add_argument("--tag", default=RELEASE_TAG)
    push.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    return parser.parse_args()


def _token_from_env() -> Optional[str]:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "PUBLISH_REPO_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def main() -> None:
    args = _parse_args()
    if args.command == "restore":
        result = restore_state_db(args.state_dir, args.target)
    elif args.command == "store":
        result = store_state_db(
            args.source,
            args.state_dir,
            chunk_bytes=args.chunk_bytes,
        )
    elif args.command == "pull":
        result = pull_state_db(
            args.repository,
            args.target,
            tag=args.tag,
            token=_token_from_env(),
        )
    else:
        result = push_state_db(
            args.source,
            args.repository,
            tag=args.tag,
            token=_token_from_env(),
            chunk_bytes=args.chunk_bytes,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
