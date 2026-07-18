from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_secret_paths_are_ignored_and_untracked() -> None:
    patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {"/data/", "/artifacts/", ".env"} <= patterns

    tracked = subprocess.run(
        ["git", "ls-files", "--", "data", "artifacts", ".env"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not tracked
