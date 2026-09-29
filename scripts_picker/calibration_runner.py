"""Run US calibration backtests in parallel on disposable DB copies.

Usage:
    python scripts_picker/calibration_runner.py <experiments.json> <results.csv>

experiments.json: [{"name": "B_8x2w", "start": "2017-01-09", "end": "2021-12-27",
                    "args": ["--rebalance-weeks", "2", "--override", "target_count=8"],
                    "weights": null}]
Each worker copies the scored source DB once and reuses it. ``weights`` (a
scoring_weights.yaml path) triggers a recompose of cached composites first.
Metrics come from model_performance (strategy vs SPY total return NAVs).
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SOURCE_DB = Path(os.environ.get("US_PICKER_CALIB_SOURCE", Path.home() / ".us-picker" / "us_picker.db"))
WORK_DIR = Path(os.environ.get("US_PICKER_CALIB_DIR", Path.home() / ".us-picker" / "calib"))
WORKERS = int(os.environ.get("US_PICKER_CALIB_WORKERS", "3"))
REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


def metrics(db: Path) -> dict:
    con = sqlite3.connect(db)
    rows = con.execute(
        "select date, strategy_return, benchmark_return from model_performance order by date"
    ).fetchall()
    con.close()
    if len(rows) < 3:
        return {}
    strat = [r[1] for r in rows]
    bench = [r[2] for r in rows]
    from datetime import date

    years = (date.fromisoformat(rows[-1][0]) - date.fromisoformat(rows[0][0])).days / 365.25
    rets = [strat[i] / strat[i - 1] - 1 for i in range(1, len(strat))]
    periods_per_year = (len(rets) / years) if years > 0 else 26
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1))
    peak, mdd = strat[0], 0.0
    for v in strat:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    bpeak, bmdd = bench[0], 0.0
    for v in bench:
        bpeak = max(bpeak, v)
        bmdd = min(bmdd, v / bpeak - 1)
    total = strat[-1] / strat[0] - 1
    btotal = bench[-1] / bench[0] - 1
    return {
        "total_pct": round(total * 100, 1),
        "spy_pct": round(btotal * 100, 1),
        "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if years > 0 else None,
        "spy_cagr_pct": round(((1 + btotal) ** (1 / years) - 1) * 100, 2) if years > 0 else None,
        "sharpe": round(mean / sd * math.sqrt(periods_per_year), 2) if sd > 0 else None,
        "maxdd_pct": round(mdd * 100, 1),
        "spy_maxdd_pct": round(bmdd * 100, 1),
    }


def run(worker_db: Path, exp: dict) -> dict:
    env = dict(os.environ, US_PICKER_DB_PATH=str(worker_db), CI="1", PYTHONIOENCODING="utf-8")
    log = WORK_DIR / f"{exp['name']}.log"
    with log.open("w", encoding="utf-8") as handle:
        if exp.get("weights"):
            subprocess.run(
                [PY, str(REPO / "scripts_picker" / "recompose_composite.py"), exp["weights"]],
                cwd=REPO, env=env, stdout=handle, stderr=subprocess.STDOUT, check=True,
            )
        cmd = [PY, "-m", "us_picker", "backtest", "--start-date", exp["start"],
               "--end-date", exp["end"], "--full-reset", "--execution", "same-day-open", *exp["args"]]
        proc = subprocess.run(cmd, cwd=REPO, env=env, stdout=handle, stderr=subprocess.STDOUT)
    result = {"name": exp["name"], "start": exp["start"], "end": exp["end"],
              "args": " ".join(exp["args"]), "weights": exp.get("weights") or "", "exit": proc.returncode}
    result.update(metrics(worker_db))
    return result


def main() -> None:
    experiments = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    dbs = []
    for i in range(min(WORKERS, len(experiments))):
        db = WORK_DIR / f"worker{i}.db"
        if not db.exists():
            shutil.copyfile(SOURCE_DB, db)
        dbs.append(db)

    # A recomposed DB must not leak into a later experiment: weight
    # experiments get a fresh copy of the source first.
    def job(index_exp):
        index, exp = index_exp
        db = dbs[index % len(dbs)]
        if exp.get("weights"):
            fresh = WORK_DIR / f"{exp['name']}.db"
            shutil.copyfile(SOURCE_DB, fresh)
            try:
                return run(fresh, exp)
            finally:
                fresh.unlink(missing_ok=True)
        return run(db, exp)

    # Serialize experiments that share a worker DB.
    buckets: dict[int, list] = {}
    for idx, exp in enumerate(experiments):
        buckets.setdefault(idx % len(dbs), []).append((idx, exp))

    def run_bucket(items):
        return [job(item) for item in items]

    results = []
    with ThreadPoolExecutor(max_workers=len(dbs)) as pool:
        for chunk in pool.map(run_bucket, buckets.values()):
            results.extend(chunk)
    fields = ["name", "start", "end", "args", "weights", "exit", "total_pct", "spy_pct",
              "cagr_pct", "spy_cagr_pct", "sharpe", "maxdd_pct", "spy_maxdd_pct"]
    write_header = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if write_header:
            writer.writeheader()
        for row in sorted(results, key=lambda r: r["name"]):
            writer.writerow({k: row.get(k) for k in fields})
    for row in sorted(results, key=lambda r: r["name"]):
        print(row)


if __name__ == "__main__":
    main()
