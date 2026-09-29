"""Fragility / sensitivity panel for the structural backtest knobs (A2).

Advisor review 2026-07-07: the production config (rotation_weeks=2,
incumbent_min_score=88, trailing 20-35) was chosen by maximizing return on a
SINGLE 2018-2026 path. The trailing band alone swings the headline ~3.4x
(+2848% vs +852%). A robust strategy's P&L should not collapse under a modest
parameter change — so this panel perturbs each knob and reports the return
spread, flagging any knob whose result is fragile (large swing under small
perturbation).

Usage:
    PYTHONUTF8=1 python scripts/fragility_panel.py --db PATH \
        [--start 2018-03-19] [--end 2026-06-22] [--quick]

--quick runs a short window with a reduced grid to smoke-test the harness.
The FULL 2018-2026 grid is a maintenance job (each run is minutes); this script
just structures and reports it. It never persists model_performance.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.backtest.engine import BacktestEngine
from us_picker.db.connection import ensure_runtime_db_ready

# knob perturbation grids. base value is marked so the report can center on it.
GRIDS = {
    "rotation_weeks": {"base": 2, "values": [1, 2, 3, 4]},
    "incumbent_min_score": {"base": 88.0, "values": [80.0, 84.0, 88.0, 92.0]},
    "trailing_band": {
        "base": (0.20, 0.35),
        "values": [(0.10, 0.25), (0.15, 0.30), (0.20, 0.35), (0.25, 0.40)],
    },
}

QUICK_GRIDS = {
    "rotation_weeks": {"base": 2, "values": [2, 4]},
    "trailing_band": {"base": (0.20, 0.35), "values": [(0.10, 0.25), (0.20, 0.35)]},
}

FRAGILE_SWING = 2.0  # max/min growth-factor ratio above this => flagged fragile


def _run(engine_bt: BacktestEngine, start, end, *, rotation_weeks, incumbent, trail):
    overrides = None
    if incumbent is not None:
        overrides = {"index_aware": {"incumbent_min_score": incumbent}}
    df = engine_bt.run_1y_backtest(
        start_date=start,
        end_date=end,
        console=None,
        strategy_variant="index_aware",
        execution_mode="next_open",
        rebalance_every_n_weeks=rotation_weeks,
        selection_overrides=overrides,
        position_continuity=True,
        intra_window_trailing=True,
        trail_min_pct=trail[0],
        trail_max_pct=trail[1],
        persist_model_performance=False,
        export_csv=False,
    )
    s = BacktestEngine._summarize_performance(df)
    return {
        "total_return_pct": s.get("total_return_pct"),
        "cagr_pct": s.get("cagr_pct"),
        "max_drawdown_pct": s.get("max_drawdown_pct"),
        "sharpe_nominal": s.get("sharpe_nominal"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--start", default="2018-03-19")
    ap.add_argument("--end", default="2026-06-22")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    grids = QUICK_GRIDS if args.quick else GRIDS

    engine = create_engine(f"sqlite:///{args.db}")
    ensure_runtime_db_ready(engine)
    session = sessionmaker(bind=engine)()
    bt = BacktestEngine(session)

    report: dict = {"window": [args.start, args.end], "knobs": {}}
    for knob, grid in grids.items():
        rows = []
        for val in grid["values"]:
            kwargs = dict(rotation_weeks=2, incumbent=None, trail=(0.20, 0.35))
            if knob == "rotation_weeks":
                kwargs["rotation_weeks"] = val
            elif knob == "incumbent_min_score":
                kwargs["incumbent"] = val
            elif knob == "trailing_band":
                kwargs["trail"] = val
            metrics = _run(bt, start, end, **kwargs)
            rows.append({"value": val, **metrics})
            print(f"[{knob}={val}] total={metrics['total_return_pct']}% "
                  f"CAGR={metrics['cagr_pct']}% MaxDD={metrics['max_drawdown_pct']}% "
                  f"Sharpe={metrics['sharpe_nominal']}")

        growths = [
            1.0 + (r["total_return_pct"] / 100.0)
            for r in rows
            if r["total_return_pct"] is not None
        ]
        swing = (max(growths) / min(growths)) if growths and min(growths) > 0 else None
        fragile = bool(swing is not None and swing > FRAGILE_SWING)
        report["knobs"][knob] = {
            "rows": rows,
            "growth_swing_ratio": round(swing, 3) if swing else None,
            "fragile": fragile,
        }
        flag = "  <<< FRAGILE" if fragile else ""
        print(f"==> {knob}: swing×{report['knobs'][knob]['growth_swing_ratio']}{flag}\n")

    session.close()
    out = json.dumps(report, indent=2, ensure_ascii=False)
    print(out)
    return report


if __name__ == "__main__":
    main()
