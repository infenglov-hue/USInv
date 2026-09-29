"""Scoring-cache contract version.

Bump this value whenever historical factor values can change without raw rows
changing (for example annual-only -> interim TTM or a corporate-action repair).
Cached backtest scores from another version are auditable but not comparable.
"""

SCORING_PIPELINE_VERSION = "2026-09-29-us-v1"
