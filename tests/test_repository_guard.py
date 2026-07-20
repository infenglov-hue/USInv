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


def test_alpaca_smoke_is_explicitly_gated_and_uses_only_actions_secrets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "alpaca-smoke.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow and "types: [labeled]" in workflow
    assert "github.event.label.name == 'alpaca-live-smoke'" in workflow
    assert "schedule:" not in workflow
    assert "secrets.ALPACA_KEY_ID" in workflow
    assert "secrets.ALPACA_SECRET_KEY" in workflow
    assert "Require Alpaca credentials" in workflow
