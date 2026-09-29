"""Compare legacy next-open and pre-open same-day execution contracts.

Run this against a disposable database copy.  Historical T-1 score rows may
be materialized during the same-day-open case, so the script deliberately
refuses the default production database unless ``--allow-default-db`` is
passed.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from us_picker.backtest.engine import BacktestEngine
from us_picker.db.connection import get_engine, session_scope
from us_picker.portfolio.exit_rules import _load_trailing_config
from us_picker.portfolio.rotation import load_rotation_config


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", type=_parse_date, required=True)
    parser.add_argument("--end-date", type=_parse_date, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-default-db", action="store_true")
    args = parser.parse_args()

    configured_db = os.environ.get("US_PICKER_DB_PATH", "").strip()
    if not configured_db and not args.allow_default_db:
        parser.error(
            "Set US_PICKER_DB_PATH to a disposable database copy; refusing the "
            "default runtime database."
        )

    rotation_weeks, rotation_anchor = load_rotation_config()
    trailing = _load_trailing_config()
    cases: list[dict] = []
    engine = get_engine()
    with session_scope(engine) as session:
        backtester = BacktestEngine(session)
        for mode in ("next_open", "same_day_open"):
            frame = backtester.run_1y_backtest(
                start_date=args.start_date,
                end_date=args.end_date,
                strategy_variant="index_aware",
                execution_mode=mode,
                friction_round_trip=0.004,
                persist_model_performance=False,
                export_csv=False,
                rebalance_every_n_weeks=rotation_weeks,
                rebalance_anchor_date=rotation_anchor,
                position_continuity=True,
                intra_window_trailing=trailing["enabled"],
                trail_min_pct=trailing["min_pct"],
                trail_max_pct=trailing["max_pct"],
                rebuild_stale_scores=True,
            )
            summary = backtester._summarize_performance(frame)
            cases.append(
                {
                    "execution_mode": mode,
                    "points": len(frame),
                    "summary": summary,
                    "run_meta": backtester._last_run_meta,
                }
            )
            if (backtester._last_run_meta or {}).get("stale_score_cache_dates"):
                raise RuntimeError(
                    f"{mode} still used stale score-cache dates after rebuild"
                )

    report = {
        "generated_at": datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat(),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "rotation_weeks": rotation_weeks,
        "rotation_anchor": (
            rotation_anchor.isoformat() if rotation_anchor else None
        ),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
