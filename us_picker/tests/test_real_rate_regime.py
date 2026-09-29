"""Real-rate conditional alpha override (2026-07-09).

Pins the config-gated regime switch: enabled=false (default) must be a
byte-identical no-op; enabled=true swaps the alpha block only when the real
policy rate (policy − CPI YoY, PIT rows <= scoring_date) exceeds the
threshold; missing macro data or empty block never crashes composition.
"""

from datetime import date

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, MacroRegime
from us_picker.scoring.composer import ScoreComposer

BASE_ALPHA = {
    "quality_buffett": 0.15,
    "value_graham_dcf": 0.30,
    "piotroski": 0.15,
    "growth": 0.10,
    "momentum": 0.25,
    "technical": 0.05,
}
DEFENSIVE = {
    "quality_buffett": 0.25,
    "value_graham_dcf": 0.15,
    "piotroski": 0.20,
    "growth": 0.10,
    "momentum": 0.25,
    "technical": 0.05,
}


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _composer(tmp_path, enabled=True, threshold=0.0, block=DEFENSIVE, extra=None):
    cfg = {
        "alpha": BASE_ALPHA,
        "banking": {"roe": 1.0},
        "holding": {"nav_discount": 1.0},
        "ipo": {"liquidity": 1.0},
        "reit": {"roe": 1.0},
        "insurance": {"roe": 1.0},
        "real_rate_regime": {
            "enabled": enabled,
            "threshold": threshold,
            "weights": block,
        },
    }
    if extra:
        cfg.update(extra)
    path = tmp_path / "weights.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return ScoreComposer(weights_path=path)


def _seed_macro(session, d, policy, cpi):
    session.add(MacroRegime(date=d, policy_rate_pct=policy, cpi_yoy_pct=cpi))
    session.flush()


def test_disabled_is_noop(session, tmp_path):
    _seed_macro(session, date(2026, 6, 30), 0.37, 0.32)  # positive real rate
    composer = _composer(tmp_path, enabled=False)
    assert composer._real_rate_alpha_override(session, date(2026, 7, 1)) is None


def test_positive_real_rate_swaps_block(session, tmp_path):
    _seed_macro(session, date(2026, 6, 30), 0.37, 0.32)  # +5pp real
    composer = _composer(tmp_path)
    out = composer._real_rate_alpha_override(session, date(2026, 7, 1))
    assert out == DEFENSIVE


def test_negative_real_rate_keeps_base(session, tmp_path):
    _seed_macro(session, date(2022, 6, 1), 0.14, 0.79)  # deeply negative
    composer = _composer(tmp_path)
    assert composer._real_rate_alpha_override(session, date(2022, 6, 2)) is None


def test_threshold_respected(session, tmp_path):
    _seed_macro(session, date(2026, 6, 30), 0.34, 0.33)  # +1pp real
    composer = _composer(tmp_path, threshold=0.02)
    assert composer._real_rate_alpha_override(session, date(2026, 7, 1)) is None


def test_pit_only_rows_on_or_before_date(session, tmp_path):
    # Future macro row must not leak into an earlier scoring date.
    _seed_macro(session, date(2026, 7, 15), 0.37, 0.32)
    composer = _composer(tmp_path)
    assert composer._real_rate_alpha_override(session, date(2026, 7, 1)) is None


def test_missing_macro_is_safe(session, tmp_path):
    composer = _composer(tmp_path)
    assert composer._real_rate_alpha_override(session, date(2026, 7, 1)) is None


def test_bad_block_sum_raises(tmp_path):
    bad = dict(DEFENSIVE)
    bad["momentum"] = 0.50  # sum > 1
    with pytest.raises(ValueError, match="real_rate_regime"):
        _composer(tmp_path, block=bad)


def test_compose_all_overlay_multipliers_tolerate_config_blocks(session, tmp_path):
    # Regression (2026-07-09): the legacy macro-overlay multiplier loop
    # iterates every weights section; the nested real_rate_regime block
    # crashed it with dict*float as soon as multipliers were active
    # (regime RISK_ON). Config blocks must be skipped like regime_weights.
    session.add(
        MacroRegime(
            date=date(2026, 6, 30),
            policy_rate_pct=0.37,
            cpi_yoy_pct=0.32,
            regime="RISK_ON",
        )
    )
    session.flush()
    composer = _composer(tmp_path)
    composer.compose_all(session, scoring_date=date(2026, 7, 1))  # must not raise
