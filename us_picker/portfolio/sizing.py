"""Risk-adjusted position sizing — REPORT ONLY (B1-P2, advisor review 2026-07-07).

The live portfolio holds equal weights (each ~1/N). A MEDIUM-risk name and a
LOW-risk name therefore carry the same weight, which is not risk parity. This
module computes *suggested* inverse-volatility weights so the user (and future
backtests) can compare equal vs risk-adjusted sizing. It does NOT change live
weights — per the disciplined "measure before shipping" rule, any live sizing
change must be A/B'd against the equal-weight baseline first.
"""

from __future__ import annotations

from typing import Optional


def inverse_vol_weights(
    vols: dict[str, float],
    *,
    floor: float = 1e-6,
    max_weight: Optional[float] = None,
) -> dict[str, float]:
    """Normalized inverse-volatility weights (higher vol → smaller weight).

    Names with missing/zero vol fall back to the median vol so they are not
    handed an infinite weight. When ``max_weight`` is set, weights are capped
    and the excess redistributed to the uncapped names (one pass — good enough
    for a report). Returns equal weights if no usable vol is present.
    """
    tickers = list(vols.keys())
    if not tickers:
        return {}

    usable = [v for v in vols.values() if v is not None and v > floor]
    if not usable:
        equal = 1.0 / len(tickers)
        return {t: equal for t in tickers}

    median_vol = sorted(usable)[len(usable) // 2]
    inv = {
        t: 1.0 / max(floor, (v if (v is not None and v > floor) else median_vol))
        for t, v in vols.items()
    }
    total = sum(inv.values())
    weights = {t: w / total for t, w in inv.items()}

    if max_weight is not None and 0 < max_weight < 1.0:
        capped = {t: min(w, max_weight) for t, w in weights.items()}
        deficit = 1.0 - sum(capped.values())
        headroom = {
            t: (max_weight - capped[t]) for t in capped if capped[t] < max_weight
        }
        room_total = sum(headroom.values())
        if deficit > 1e-9 and room_total > 1e-9:
            for t, room in headroom.items():
                capped[t] += deficit * (room / room_total)
        weights = capped

    return weights


def risk_parity_report(positions: list[dict]) -> list[dict]:
    """Per-position equal vs inverse-vol weight comparison (report only).

    Each position dict needs ``ticker`` and ``volatility`` (annualized, e.g.
    0.35 = 35%). Returns rows with equal_weight, risk_weight and their delta,
    so a caller can render "you are X pp over/under-weight vs risk parity".
    """
    if not positions:
        return []
    n = len(positions)
    equal = 1.0 / n
    vols = {p["ticker"]: p.get("volatility") for p in positions}
    risk_weights = inverse_vol_weights(vols)
    rows: list[dict] = []
    for p in positions:
        t = p["ticker"]
        rw = risk_weights.get(t, equal)
        rows.append(
            {
                "ticker": t,
                "volatility": p.get("volatility"),
                "equal_weight_pct": round(equal * 100.0, 2),
                "risk_weight_pct": round(rw * 100.0, 2),
                "delta_pct": round((rw - equal) * 100.0, 2),
            }
        )
    return rows
