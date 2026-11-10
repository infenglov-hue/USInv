"""Run US Picker experiments against a single, isolated SQLite snapshot.

Usage: python scripts_picker/calibration_runner.py experiments.json results.csv

Each JSON entry supplies name, start, end, args, and optional weights (a YAML
file relative to the experiment definition, or an absolute path). See
calibration/v2-train.json. Reports and checksums are saved beside the CSV.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

SOURCE_DB = Path(os.environ.get("US_PICKER_CALIB_SOURCE", Path.home() / ".us-picker" / "us_picker.db"))
WORK_DIR = Path(os.environ.get("US_PICKER_CALIB_DIR", Path.home() / ".us-picker" / "calib"))
WORKERS = int(os.environ.get("US_PICKER_CALIB_WORKERS", "3"))
REPO = Path(__file__).resolve().parent.parent
FIELDS = [
    "name", "start", "end", "args", "weights", "exit", "total_pct", "spy_pct",
    "cagr_pct", "spy_cagr_pct", "sharpe", "maxdd_pct", "spy_maxdd_pct",
]


def metrics(nav_path: Path) -> dict:
    with nav_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = [date.fromisoformat(row["date"]) for row in rows]
    if len(dates) < 3 or dates != sorted(set(dates)):
        raise ValueError("expected at least three distinct NAV dates in order")
    strat = [float(row["strategy_return"]) for row in rows]
    bench = [float(row["benchmark_return"]) for row in rows]
    if not all(math.isfinite(value) and value > 0 for value in strat + bench):
        raise ValueError("invalid strategy or benchmark NAV")
    years = (dates[-1] - dates[0]).days / 365.25
    returns = [end / start - 1 for start, end in zip(strat, strat[1:])]
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((ret - mean) ** 2 for ret in returns) / (len(returns) - 1))
    total = strat[-1] / strat[0]
    btotal = bench[-1] / bench[0]

    def drawdown(values: list[float]) -> float:
        peak, worst = values[0], 0.0
        for value in values:
            peak = max(peak, value)
            worst = min(worst, value / peak - 1)
        return round(worst * 100, 4)

    return {
        "total_pct": round((total - 1) * 100, 4),
        "spy_pct": round((btotal - 1) * 100, 4),
        "cagr_pct": round((total ** (1 / years) - 1) * 100, 4),
        "spy_cagr_pct": round((btotal ** (1 / years) - 1) * 100, 4),
        "sharpe": round(mean / sd * math.sqrt(len(returns) / years), 4) if sd > 0 else None,
        "maxdd_pct": drawdown(strat),
        "spy_maxdd_pct": drawdown(bench),
        "nav_start": dates[0].isoformat(),
        "nav_end": dates[-1].isoformat(),
        "nav_points": len(dates),
    }


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def code_fingerprint() -> dict:
    paths = ["us_picker", "usinv", "scripts_picker", "pyproject.toml"]
    code_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", *paths], cwd=REPO)
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths],
        cwd=REPO,
    ).decode("utf-8").split("\0")
    file_hashes = [(name, sha256(REPO / name)) for name in sorted(set(files)) if (REPO / name).is_file()]
    return {
        "code_sha": code_sha,
        "tracked_code_diff_sha256": hashlib.sha256(diff).hexdigest() if diff else None,
        "working_code_sha256": hashlib.sha256(json.dumps(file_hashes).encode("utf-8")).hexdigest(),
    }


def run(source: Path, exp: dict, run_dir: Path) -> dict:
    report = run_dir / exp["name"]
    report.mkdir()
    result = {
        "name": exp["name"], "start": exp["start"], "end": exp["end"],
        "args": " ".join(exp["args"]), "weights": exp.get("weights") or "",
    }
    with TemporaryDirectory(prefix=exp["name"] + "-", dir=source.parent) as temp:
        worker_db = Path(temp) / "research.db"
        shutil.copyfile(source, worker_db)
        env = dict(os.environ, US_PICKER_DB_PATH=str(worker_db), CI="1", PYTHONIOENCODING="utf-8")
        cmd = [
            sys.executable, "-m", "us_picker", "backtest", *exp["args"],
            "--start-date", exp["start"], "--end-date", exp["end"],
            "--execution", "same-day-open", "--research-dir", str(report),
        ]
        if exp.get("weights"):
            shutil.copyfile(exp["weights"], report / "weights.yaml")
            cmd.extend(["--weights-file", str(report / "weights.yaml")])
        with (report / "backtest.log").open("w", encoding="utf-8") as log:
            proc = subprocess.run(cmd, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    result["exit"] = proc.returncode
    if proc.returncode == 0:
        try:
            if json.loads((report / "run.json").read_text(encoding="utf-8"))["stale_score_cache_dates"] != 0:
                raise ValueError("stale score cache dates")
            result.update(metrics(report / "nav.csv"))
        except (OSError, ValueError, KeyError) as exc:
            result.update(exit=1, error=str(exc))
    result["argv"] = cmd[3:]
    result["artifacts_sha256"] = {
        path.name: sha256(path) for path in sorted(report.iterdir()) if path.is_file()
    }
    return result


def main() -> None:
    if len(sys.argv) != 3 or WORKERS < 1:
        raise SystemExit(__doc__)
    plan = Path(sys.argv[1]).resolve()
    experiments = json.loads(plan.read_text(encoding="utf-8"))
    if not isinstance(experiments, list) or not experiments:
        raise SystemExit("The calibration plan must have at least one experiment.")
    names = set()
    for exp in experiments:
        name = exp["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
            raise SystemExit(f"Invalid or duplicate experiment name: {name!r}")
        names.add(name)
        if date.fromisoformat(exp["start"]) >= date.fromisoformat(exp["end"]):
            raise SystemExit(f"Experiment {name} must start before its end date.")
        if exp.get("weights"):
            exp["weights"] = str((plan.parent / exp["weights"]).resolve(strict=True))

    out = Path(sys.argv[2]).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=f"{out.stem}-", dir=out.parent))
    print(f"Calibration artifacts: {run_dir}", flush=True)
    code_before = code_fingerprint()
    plan_sha256 = sha256(plan)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="calibration-", dir=WORK_DIR) as temp:
        snapshot = Path(temp) / "snapshot.db"
        with (
            closing(sqlite3.connect(SOURCE_DB.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as destination,
        ):
            source.backup(destination)
        data_sha256 = sha256(snapshot)
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(experiments))) as pool:
            results = list(pool.map(lambda exp: run(snapshot, exp, run_dir), experiments))

    code_changed = code_before["working_code_sha256"] != code_fingerprint()["working_code_sha256"]
    manifest = {
        **code_before,
        "code_changed_during_run": code_changed,
        "source_snapshot_sha256": data_sha256,
        "experiment_plan_sha256": plan_sha256,
        "runs": results,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if code_changed:
        raise SystemExit("Code changed during calibration. The results were not added to the summary CSV.")
    write_header = not out.exists() or out.stat().st_size == 0
    with out.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(results)
    for row in results:
        print({key: row.get(key) for key in FIELDS})
    if any(row["exit"] != 0 for row in results):
        raise SystemExit("One or more calibration runs failed. See backtest.log in the artifacts.")


if __name__ == "__main__":
    main()
