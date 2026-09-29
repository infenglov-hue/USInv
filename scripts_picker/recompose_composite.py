"""Faithful recompose: call the real ScoreComposer.compose_all for every
scoring date in a scored DB (same path _ensure_scores_for_date uses:
use_regime=False, legacy macro overlay, ATR stop/target). Used to A/B a
scoring_weights.yaml change against a cached-score archive WITHOUT rescoring
factors — only composite_alpha is recomputed, then `bist backtest` reads it.

Usage:
    US_PICKER_DB_PATH=<scored_db> python scripts/recompose_composite.py [weights.yaml]

Pass the weights YAML EXPLICITLY. When this script is run by path, sys.path[0]
is the script's dir (not the CWD), so `import us_picker` may resolve to the
installed/main copy and silently load its weights — the explicit path removes
that ambiguity.

A4 walk-forward OOS (2026-07-05) used this to reject value-tilted composites:
they beat current in 2018-2022 but went negative-alpha out-of-sample in
2024-2026. See docs/factor_attribution_2026-07.md.
"""
import sys
from pathlib import Path

from us_picker.db.connection import ensure_runtime_db_ready, get_engine, get_session
from us_picker.db.schema import ScoringResult
from us_picker.scoring.composer import ScoreComposer

# Explicit weights path (argv[1]) so the loaded weights don't depend on which
# us_picker copy import resolution picked up (run-by-path puts the script
# dir, not the CWD, on sys.path[0] — that silently loaded the installed/MAIN
# weights in the first attempt).
weights_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
ensure_runtime_db_ready(get_engine())
session = get_session()
composer = ScoreComposer(weights_path=weights_path) if weights_path else ScoreComposer()
print(f"weights_path: {weights_path}")
print(f"alpha weights: {composer.weights.get('alpha')}")

dates = [
    d[0]
    for d in session.query(ScoringResult.scoring_date)
    .distinct()
    .order_by(ScoringResult.scoring_date)
    .all()
]
for i, d in enumerate(dates):
    composer.compose_all(session, scoring_date=d, use_regime=False)
    if (i + 1) % 40 == 0:
        print(f"  {i+1}/{len(dates)} dates")
print("Done.")
session.close()
