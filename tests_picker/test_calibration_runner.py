from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts_picker import calibration_runner


@pytest.mark.parametrize(("returncode", "stale", "expected"), [(1, 0, 1), (0, 2, 1), (0, 0, 0)])
def test_calibration_reports_only_completed_fresh_nav(tmp_path, monkeypatch, returncode, stale, expected):
    source = tmp_path / "source.db"
    source.write_bytes(b"snapshot")
    exp = {"name": "train", "start": "2020-01-01", "end": "2020-03-01", "args": []}

    def backtest(cmd, *, cwd, env, stdout, stderr):
        assert cwd == calibration_runner.REPO
        worker = Path(env["US_PICKER_DB_PATH"])
        assert worker != source and worker.read_bytes() == source.read_bytes()
        worker.write_bytes(b"changed")
        report = Path(cmd[cmd.index("--research-dir") + 1])
        (report / "run.json").write_text('{"stale_score_cache_dates": %d}' % stale, encoding="utf-8")
        (report / "nav.csv").write_text(
            "date,strategy_return,benchmark_return\n"
            "2020-01-01,100,100\n2020-02-01,102,101\n2020-03-01,104,103\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(calibration_runner.subprocess, "run", backtest)
    result = calibration_runner.run(source, exp, tmp_path)
    assert result["exit"] == expected
    assert source.read_bytes() == b"snapshot"
    assert not list(tmp_path.glob("train-*"))
    if expected:
        assert "total_pct" not in result
    else:
        assert result["total_pct"] == 4
        assert result["spy_pct"] == 3
        assert result["nav_points"] == 3
    assert "nav.csv" in result["artifacts_sha256"]
