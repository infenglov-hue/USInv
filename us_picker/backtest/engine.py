"""Point-in-Time Backtest Engine for BIST Stock Picker.

Calculates the historical performance of the Top-5 strategy 
ensuring strict data isolation (no look-ahead bias).
"""

import json
import logging
import pandas as pd
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    ScoringResult,
    Company,
    DailyPrice,
    ModelPerformance
)
from us_picker.portfolio.selector import PortfolioSelector
from us_picker.scoring.version import SCORING_PIPELINE_VERSION
from us_picker.utils.index_prices import (
    get_price_splice_factor_by_ticker,
    get_spliced_price_by_ticker,
)

logger = logging.getLogger(__name__)

# L1 (2026-07-10): compact daily summary artifact consumed by mobile_feed so
# the PWA decision card can show REAL (CPI/USD/gram-gold) returns instead of
# nominal-only numbers. Written by every persist_model_performance run (the
# daily CI backtest), read fail-soft at export time.
DAILY_SUMMARY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "output"
    / "backtest_daily_summary.json"
)


def persist_daily_summary(
    summary: dict,
    start_date: date,
    end_date: date,
    path: Optional[Path] = None,
) -> Path:
    """Write the compact nominal + deflated summary for the mobile feed.

    Only the fields the PWA needs are serialized; deflated units whose
    deflator series were unavailable come through as None and are skipped by
    the consumer. Never raises into the backtest itself — callers wrap it.
    """
    target = Path(path) if path is not None else DAILY_SUMMARY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    deflated_src = summary.get("deflated") or {}

    def _unit(name: str) -> Optional[dict]:
        block = deflated_src.get(name) or {}
        if not isinstance(block, dict) or block.get("total_return_pct") is None:
            return None
        return {
            "total_return_pct": block.get("total_return_pct"),
            "cagr_pct": block.get("cagr_pct"),
            "max_drawdown_pct": block.get("max_drawdown_pct"),
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "nominal": {
            "total_return_pct": summary.get("total_return_pct"),
            "benchmark_return_pct": summary.get("benchmark_return_pct"),
            "alpha_pct": summary.get("alpha_pct"),
            "cagr_pct": summary.get("cagr_pct"),
            "sharpe": summary.get("sharpe_nominal"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
        },
        "deflated": {
            "cpi_real": _unit("cpi_real"),
            "usd": _unit("usd"),
            "gram_gold": _unit("gram_gold"),
        },
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def _pick_has_quality_flag(pick: dict[str, Any], flag: str) -> bool:
    payload = pick.get("quality_flags_json")
    if not payload:
        return False
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(payload, list):
        return False
    return flag in {str(item) for item in payload}


def _avg_pct(total_return: float, count: int) -> float | None:
    if count <= 0:
        return None
    return round((total_return / count) * 100.0, 4)


def _load_backtest_config() -> dict:
    """Read the `backtest` execution-realism block from thresholds.yaml.

    Every default reproduces the current published numbers exactly (idle yield
    off, zero extra costs, liquidity impact off), so a stock run is unchanged
    unless the user opts in (A4 / #9, 2026-07-07).
    """
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = (yaml.safe_load(f) or {}).get("backtest", {}) or {}
    except OSError:
        cfg = {}
    impact = cfg.get("liquidity_impact", {}) or {}
    return {
        "idle_cash_yield": str(cfg.get("idle_cash_yield", "none")).strip().lower(),
        "commission_pct_per_side": float(cfg.get("commission_pct_per_side", 0.0) or 0.0),
        "bsmv_pct_of_commission": float(cfg.get("bsmv_pct_of_commission", 0.0) or 0.0),
        "impact_enabled": bool(impact.get("enabled", False)),
        "impact_portfolio_try": float(impact.get("portfolio_try", 1_000_000) or 0.0),
        "impact_coef": float(impact.get("impact_coef", 0.10) or 0.0),
        "impact_adv_lookback_days": int(impact.get("adv_lookback_days", 30) or 30),
        "impact_participation_cap": float(impact.get("participation_cap", 0.20) or 0.20),
    }


class BacktestEngine:
    """Calculates historical strategy performance."""

    def __init__(self, session: Session):
        self.session = session
        self._gold_cache = None
        # A1 (2026-07-07): deflator series so the strategy NAV can also be
        # reported in gram-gold, USD and CPI-real (purchasing-power) terms.
        # Nominal-TRY headline numbers are misleading over a hyperinflation /
        # currency-collapse window; these let _summarize_performance emit the
        # real edge. Loaded once per backtest run (cheap, cached in memory).
        self._usd_cache = None
        self._cpi_cache = None
        self._bt_cfg: dict = {}
        self._last_run_meta: Optional[dict] = None

    # Round-trip trading friction: 0.2% buy + 0.2% sell = 0.4% total.
    # Applied ONLY to newly bought stocks, NOT to stocks held from the
    # previous week (turnover-aware friction).
    _FRICTION_ROUND_TRIP: float = 0.004  # 0.4% per new position
    _EXECUTION_SAME_DAY_CLOSE = "same_day_close"
    _EXECUTION_SAME_DAY_OPEN = "same_day_open"
    _EXECUTION_NEXT_OPEN = "next_open"
    _VALID_EXECUTION_MODES = {
        _EXECUTION_SAME_DAY_CLOSE,
        _EXECUTION_SAME_DAY_OPEN,
        _EXECUTION_NEXT_OPEN,
    }

    # Annual expense ratio applied to the benchmark (BIST100 index ETF
    # proxy) so the comparison is fair — strategy pays friction, benchmark
    # pays management fees.
    _BENCHMARK_ANNUAL_EXPENSE: float = 0.005  # 0.5% per year

    def run_1y_backtest(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        console=None,
        *,
        initial_strategy_nav: float = 100.0,
        initial_benchmark_nav: float = 100.0,
        previous_tickers: Optional[set[str]] = None,
        strategy_variant: str = "index_aware",
        execution_mode: str = _EXECUTION_NEXT_OPEN,
        friction_round_trip: Optional[float] = None,
        persist_model_performance: bool = True,
        export_csv: bool = True,
        output_suffix: Optional[str] = None,
        export_dir: Optional[Path] = None,
        rebalance_every_n_weeks: int = 1,
        rebalance_anchor_date: Optional[date] = None,
        selection_overrides: Optional[dict] = None,
        position_continuity: bool = True,
        intra_window_trailing: bool = False,
        trail_min_pct: float = 0.10,
        trail_max_pct: float = 0.25,
        rebuild_stale_scores: bool = False,
    ) -> pd.DataFrame:
        """Run a backtest of the ALPHA (Top 5) strategy. Default 3 years (156 weeks).

        Zero Look-Ahead Rule:
        For each Monday, we only use the model's scores that were calculated
        ON or BEFORE that weekend, based on financials available at THAT time.

        Fixes applied (2026-05-26):
          - Friction only on NEW buys (round-trip 0.4%), not on held stocks.
          - Cash-signal integration: equity exposure scaled by (1 - cash_pct).
          - Benchmark gets annual expense ratio for fair comparison.
          - Previous portfolio tracked to identify new vs held positions.

        position_continuity (B1, 2026-07-05 — default True, matches live):
          Held positions carry their stop across rebalances (never re-anchored
          down). ``False`` restores the pre-B1 behavior where every rebalance
          reset the stop to the fresh ATR level — kept for A/B comparison.

        intra_window_trailing: additionally ratchet the stop off the highest
          daily close INSIDE the holding window. Production uses the 20-35%
          clamp (2026-07-05 A/B: +2848%, MaxDD −20.6, best Sharpe). NEVER
          use tight clamps — the 10-25% variant collapsed the same backtest
          to +852%. The CLI resolves these three args from
          ``selection.trailing_stop`` in thresholds.yaml so `bist backtest`
          matches live behavior by default.
        """
        execution_mode = self._normalize_execution_mode(execution_mode)
        applied_friction = (
            self._FRICTION_ROUND_TRIP
            if friction_round_trip is None
            else max(0.0, float(friction_round_trip))
        )
        rebalance_every_n_weeks = max(1, int(rebalance_every_n_weeks))

        # A4/#9: execution-realism knobs (all default to current behavior).
        self._bt_cfg = _load_backtest_config()
        idle_mode = self._bt_cfg["idle_cash_yield"]
        # Turkish round-trip transaction cost (commission + BSMV on commission),
        # ADDED to the base friction on new buys. Zero by default.
        cost_round_trip = (
            2.0
            * self._bt_cfg["commission_pct_per_side"]
            * (1.0 + self._bt_cfg["bsmv_pct_of_commission"])
        )
        applied_friction = applied_friction + cost_round_trip
        impact_enabled = self._bt_cfg["impact_enabled"]
        capacity_samples: list[float] = []
        stale_score_cache_dates = 0

        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(weeks=156)
            # Align start_date to the first Monday on or after it
            while start_date.weekday() != 0:
                start_date += timedelta(days=1)

        # 1. Identify Monday rebalance dates (every Nth Monday for lower
        # rotation frequencies — C1 calibration).
        rebalance_dates = _build_rebalance_dates(
            start_date,
            end_date,
            rebalance_every_n_weeks,
            anchor_date=rebalance_anchor_date,
        )

        signal_dates_by_effective: dict[date, date] = {
            day: day for day in rebalance_dates
        }
        if execution_mode == self._EXECUTION_SAME_DAY_OPEN:
            from us_picker.portfolio.execution import (
                latest_completed_session_date,
            )

            signal_dates_by_effective = {
                day: signal_day
                for day in rebalance_dates
                if (
                    signal_day := latest_completed_session_date(
                        self.session, day
                    )
                )
                is not None
            }
            # The first historical Monday may be the first price row in the
            # database and therefore have no legal T-1 signal. Drop it rather
            # than leak T close into a pre-open decision.
            rebalance_dates = [
                day for day in rebalance_dates if day in signal_dates_by_effective
            ]

        if not rebalance_dates:
            logger.warning("No rebalance dates found between %s and %s", start_date, end_date)
            return pd.DataFrame()

        if console:
            console.print(
                "[bold green]Running backtest "
                f"({strategy_variant}, {execution_mode}, friction={applied_friction:.2%}) "
                f"starting {rebalance_dates[0]} "
                f"to {rebalance_dates[-1]} ({len(rebalance_dates)} weeks)...[/bold green]"
            )

        try:
            import yfinance as yf
            if console:
                console.print("[dim]Fetching Gold and USD/TRY prices for cash hedging...[/dim]")

            # Incremental runs may calculate only the newest point, while the
            # published summary is rebuilt from the complete persisted NAV.
            # Fetch deflators from the same earliest date or old NAV points
            # would all receive the first recent FX/gold value and silently
            # reproduce nominal returns in the USD/gold fields.
            deflator_start = self._deflator_history_start(
                start_date,
                persist_model_performance=persist_model_performance,
            )
            gold = yf.download(
                "GC=F",
                start=deflator_start.isoformat(),
                end=(end_date + timedelta(days=7)).isoformat(),
                progress=False,
            )
            usd_try = yf.download(
                "TRY=X",
                start=deflator_start.isoformat(),
                end=(end_date + timedelta(days=7)).isoformat(),
                progress=False,
            )
            
            if isinstance(gold.columns, pd.MultiIndex):
                gold_close = gold['Close']["GC=F"]
            else:
                gold_close = gold['Close']
                
            if isinstance(usd_try.columns, pd.MultiIndex):
                usd_close = usd_try['Close']["TRY=X"]
            else:
                usd_close = usd_try['Close']
                
            df_gold = pd.DataFrame({'gold_usd': gold_close, 'usd_try': usd_close})
            df_gold = df_gold.ffill()
            df_gold['gram_gold_try'] = (df_gold['gold_usd'] / 31.1034768) * df_gold['usd_try']
            self._gold_cache = df_gold['gram_gold_try'].dropna()
            # A1: keep USD/TRY too so the strategy NAV can be expressed in USD.
            self._usd_cache = df_gold['usd_try'].dropna()
        except Exception as e:
            if console:
                console.print(f"[yellow]Failed to fetch gold data: {e}[/yellow]")
            self._gold_cache = None
            self._usd_cache = None

        # A1: CPI index series (TCMB) for purchasing-power (real) deflation.
        # Optional — absent/empty history just means the CPI-real column stays
        # blank; the run does not fail.
        self._cpi_cache = self._load_cpi_cache()

        # Clean previous backtest results ONLY for the dates we are recalculating
        if persist_model_performance and rebalance_dates:
            self.session.query(ModelPerformance).filter(ModelPerformance.date >= rebalance_dates[0].isoformat()).delete()
            self.session.commit()

        results = []
        weekly_details = [] # Store detailed metrics for CSV export
        exit_events = [] # Store stop-loss / take-profit events for audit
        # Track the current portfolio as a set of tickers for turnover detection.
        # Incremental runs receive the previous week's holdings so the first
        # appended week does not charge every incumbent as a fresh purchase.
        prev_tickers: set[str] = set(previous_tickers or set())
        portfolio = []  # List of company dicts currently held: {"company_id": int, "ticker": str}
        cooldown_dict: dict[str, int] = {}  # ticker -> weeks_remaining
        # B1 continuity: per-ticker trailing state carried across rebalances.
        # {"stop": float | None, "high": float} — stop only ever ratchets up.
        held_state: dict[str, dict] = {}
        
        strategy_nav = float(initial_strategy_nav)
        bist100_nav = float(initial_benchmark_nav)

        previous_bist_price = self._get_price("SPY", rebalance_dates[0])

        # Record initial point
        results.append({
            "date": rebalance_dates[0].isoformat(),
            "strategy_return": strategy_nav,
            "benchmark_return": bist100_nav,
            "alpha": strategy_nav - bist100_nav,
            **self._deflator_snapshot(rebalance_dates[0]),
        })
        if persist_model_performance:
            self._save_performance_row(rebalance_dates[0].isoformat(), strategy_nav, bist100_nav)
            self.session.commit()  # Commit initial point before loop (cash signal may rollback)

        for i in range(len(rebalance_dates) - 1):
            d = rebalance_dates[i]
            next_d = rebalance_dates[i+1]
            signal_date = signal_dates_by_effective[d]

            # Decrement cooldowns in weeks (one iteration spans N weeks)
            for t in list(cooldown_dict.keys()):
                cooldown_dict[t] -= rebalance_every_n_weeks
                if cooldown_dict[t] <= 0:
                    del cooldown_dict[t]

            if console:
                console.print(f"Rebalancing for week {d.isoformat()} -> {next_d.isoformat()}...")

            # A. Ensure point-in-time scores exist for the legal signal
            # cutoff. same_day_open uses the last completed session (T-1)
            # and executes at the first available open on/after T.
            score_cache_is_current = _ensure_scores_for_date(
                self.session,
                signal_date,
                console=console,
                rebuild_stale_scores=rebuild_stale_scores,
            )
            if not score_cache_is_current:
                stale_score_cache_dates += 1

            # B. Evaluate cash signal for this date
            cash_pct = self._get_cash_pct(signal_date)
            invested_fraction = max(0.0, 1.0 - cash_pct)

            # C. Rebalance: Get Top 5 candidates available AT THIS DATE
            selector = PortfolioSelector(
                scoring_date=signal_date,
                selection_date=d,
                strategy_variant=strategy_variant,
                selection_overrides=selection_overrides,
            )
            # Use current_holdings to simulate turnover-penalty calculation
            current_holdings_ids = [p["company_id"] for p in portfolio] if portfolio else None
            new_picks = selector.select(
                "ALPHA", self.session, current_holdings=current_holdings_ids, exclude_tickers=set(cooldown_dict.keys())
            )
            
            # Build the set of new tickers for this week
            new_tickers = {p["ticker"] for p in new_picks}

            # D. Calculate return from d to next_d using the picks active during this week
            portfolio_return = 0.0
            avg_equity_return = 0.0
            rf_period = 0.0
            rf_annual = 0.0
            stock_details = []
            exit_event_count = 0
            stop_loss_count = 0
            take_profit_count = 0
            new_buy_count = 0
            not_entered_count = 0
            dcf_overvalued_selected_count = 0
            dcf_other_selected_count = 0
            dcf_overvalued_valid_count = 0
            dcf_other_valid_count = 0
            dcf_overvalued_return_sum = 0.0
            dcf_other_return_sum = 0.0
            dcf_overvalued_tickers: list[str] = []
            # A scoring date may legitimately have no eligible names (for
            # example, before enough point-in-time history exists).  Treat the
            # equity sleeve as idle cash for that period and keep diagnostics
            # well-defined instead of failing while exporting weekly details.
            n_valid = 0

            if new_picks:
                total_pct_change = 0.0
                for pick in new_picks:
                    is_dcf_overvalued = _pick_has_quality_flag(pick, "DCF_OVERVALUED")
                    if is_dcf_overvalued:
                        dcf_overvalued_selected_count += 1
                        dcf_overvalued_tickers.append(pick["ticker"])
                    else:
                        dcf_other_selected_count += 1

                    p1, entry_date = self._get_entry_price(
                        pick["ticker"],
                        d,
                        next_d,
                        execution_mode,
                    )
                    if p1 <= 0:
                        # Order never filled (no print inside the window).
                        # The slot sits in cash for the period, no friction,
                        # still counted in the average so the missing fill is
                        # not silently redistributed to the remaining names
                        # (B3 execution realism). #9: that idle cash earns the
                        # configured yield (0% by default) instead of nothing.
                        idle_ret = self._idle_yield(idle_mode, d, next_d)
                        total_pct_change += idle_ret
                        not_entered_count += 1
                        n_valid += 1
                        if is_dcf_overvalued:
                            dcf_overvalued_valid_count += 1
                            dcf_overvalued_return_sum += idle_ret
                        else:
                            dcf_other_valid_count += 1
                            dcf_other_return_sum += idle_ret
                        stock_details.append(f"{pick['ticker']} (NOT_ENTERED)")
                        continue

                    stop_loss = pick.get("stop_loss")
                    take_profit = pick.get("target_price")

                    trail_state: Optional[dict] = None
                    if position_continuity:
                        # Trail distance from this pick's ATR-based stop gap,
                        # clamped like the live updater.
                        ref = pick.get("entry_price")
                        if ref and stop_loss and ref > 0 and 0 < stop_loss < ref:
                            trail_pct = min(
                                trail_max_pct,
                                max(trail_min_pct, 1.0 - (stop_loss / ref)),
                            )
                        else:
                            trail_pct = trail_max_pct
                        carried = held_state.get(pick["ticker"])
                        if carried is not None:
                            # Held position: never re-anchor the stop down.
                            carried_stop = carried.get("stop")
                            candidates = [s for s in (carried_stop, stop_loss) if s]
                            stop_loss = max(candidates) if candidates else None
                            high = max(carried.get("high") or p1, p1)
                        else:
                            high = p1
                        trail_state = {"pct": trail_pct, "high": high, "stop": stop_loss}

                    # Phase E enhancement: Intra-week stop-loss / take-profit execution
                    p2, exited, exit_reason, exit_date = self._get_weekly_exit(
                        pick["ticker"],
                        entry_date,
                        next_d,
                        stop_loss,
                        take_profit,
                        include_start_date=(
                            execution_mode
                            in {
                                self._EXECUTION_NEXT_OPEN,
                                self._EXECUTION_SAME_DAY_OPEN,
                            }
                        ),
                        trail_state=trail_state if intra_window_trailing else None,
                    )

                    if position_continuity:
                        if exited:
                            held_state.pop(pick["ticker"], None)
                        elif trail_state is not None:
                            held_state[pick["ticker"]] = {
                                "stop": trail_state.get("stop"),
                                "high": trail_state.get("high"),
                            }
                    
                    if exited and exit_reason == "STOP_LOSS":
                        cooldown_dict[pick["ticker"]] = 4  # 4 weeks (approx 1 month) cooldown
                        stop_loss_count += 1
                    elif exited and exit_reason == "TAKE_PROFIT":
                        take_profit_count += 1
                    elif exited and exit_reason == "NO_PRICE_FORCED_EXIT":
                        # Suspended/delisted mid-hold: keep it out of the
                        # candidate pool while its scores are stale.
                        cooldown_dict[pick["ticker"]] = 4
                    if exited:
                        exit_event_count += 1

                    if p1 > 0:
                        raw_return = (p2 / p1) - 1.0
                        # Friction ONLY on newly bought stocks (not held from last week)
                        is_new_buy = pick["ticker"] not in prev_tickers
                        if is_new_buy:
                            new_buy_count += 1
                        # A4: liquidity-scaled market impact on NEW buys — thin
                        # names cost more at larger AUM. Also sample the capacity
                        # (max AUM keeping this slot within the participation cap).
                        impact = 0.0
                        if impact_enabled and is_new_buy:
                            n_pos = max(1, len(new_picks))
                            slot_weight = invested_fraction / n_pos
                            trade_try = self._bt_cfg["impact_portfolio_try"] * slot_weight
                            adv = self._get_avg_turnover_try(
                                pick["company_id"], signal_date,
                                self._bt_cfg["impact_adv_lookback_days"]
                            )
                            if adv > 0 and trade_try > 0:
                                participation = trade_try / adv
                                impact = 2.0 * self._bt_cfg["impact_coef"] * participation
                                cap = self._bt_cfg["impact_participation_cap"]
                                if cap > 0 and slot_weight > 0:
                                    capacity_samples.append(adv * cap / slot_weight)
                        friction = (applied_friction + impact) if is_new_buy else 0.0
                        net_return = raw_return - friction
                        # #9: capital freed by an intra-window exit earns the
                        # idle yield for the remainder of the period (0% default).
                        if exited and idle_mode != "none" and exit_date is not None and exit_date < next_d:
                            idle_remainder = self._idle_yield(idle_mode, exit_date, next_d)
                            net_return = (1.0 + net_return) * (1.0 + idle_remainder) - 1.0
                        total_pct_change += net_return
                        n_valid += 1
                        if is_dcf_overvalued:
                            dcf_overvalued_valid_count += 1
                            dcf_overvalued_return_sum += net_return
                        else:
                            dcf_other_valid_count += 1
                            dcf_other_return_sum += net_return
                        
                        exit_tag = f" [{exit_reason}]" if exited else ""
                        stock_details.append(f"{pick['ticker']} ({p1:.2f}->{p2:.2f} | %{net_return*100:.1f}){exit_tag}")

                        if exited:
                            exit_events.append({
                                "Start_Date": d.isoformat(),
                                "End_Date": next_d.isoformat(),
                                "Entry_Date": entry_date.isoformat(),
                                "Exit_Date": (
                                    exit_date.isoformat()
                                    if exit_date is not None
                                    else next_d.isoformat()
                                ),
                                "Ticker": pick["ticker"],
                                "Reason": exit_reason,
                                "Entry_Price": round(p1, 4),
                                "Exit_Price": round(p2, 4),
                                "Stop_Loss": (
                                    round(float(stop_loss), 4)
                                    if stop_loss is not None
                                    else None
                                ),
                                "Take_Profit": (
                                    round(float(take_profit), 4)
                                    if take_profit is not None
                                    else None
                                ),
                                "Net_Return_Pct": round(net_return * 100.0, 4),
                                "Was_New_Buy": bool(is_new_buy),
                                "Strategy": strategy_variant,
                            })

                if n_valid > 0:
                    avg_equity_return = total_pct_change / n_valid

            # Phase E enhancement: Cash portion earns Gram Gold return instead of TL repo
            if cash_pct > 0:
                gold_p1 = self._get_gram_gold_price(d)
                gold_p2 = self._get_gram_gold_price(next_d)
                if gold_p1 > 0 and gold_p2 > 0:
                    rf_period = (gold_p2 / gold_p1) - 1.0
                    rf_annual = rf_period * 52  # Just for display proxy
                else:
                    # Fallback to repo if gold data fails
                    rf_annual = self._get_risk_free_rate(d)
                    days_held = (next_d - d).days
                    rf_period = (1.0 + rf_annual) ** (days_held / 365.25) - 1.0

            # Total portfolio return combines equity performance and cash interest
            portfolio_return = (avg_equity_return * invested_fraction) + (rf_period * cash_pct)

            # Update strategy NAV
            strategy_nav = strategy_nav * (1.0 + portfolio_return)

            # Update benchmark NAV multiplicatively from the previous point.
            # This keeps incremental runs continuous instead of resetting the
            # benchmark to 100 whenever only the newest weeks are recomputed.
            curr_bist_price = self._get_price("SPY", next_d)
            if previous_bist_price > 0 and curr_bist_price > 0:
                weekly_benchmark_return = curr_bist_price / previous_bist_price
                # Expense scales with the actual period length so multi-week
                # rebalance intervals charge the same annualized fee.
                period_benchmark_expense = (
                    self._BENCHMARK_ANNUAL_EXPENSE * (next_d - d).days / 365.25
                )
                bist100_nav = (
                    bist100_nav
                    * weekly_benchmark_return
                    * (1.0 - period_benchmark_expense)
                )
                previous_bist_price = curr_bist_price

            # Store detailed metrics for CSV
            weekly_details.append({
                "Start_Date": d.isoformat(),
                "End_Date": next_d.isoformat(),
                "Signal_Date": signal_date.isoformat(),
                "Execution_Mode": execution_mode,
                "Friction_Round_Trip_Pct": round(applied_friction * 100, 4),
                "Initial_NAV": round(strategy_nav / (1.0 + portfolio_return), 2),
                "Ending_NAV": round(strategy_nav, 2),
                "Cash_Ratio_Pct": round(cash_pct * 100, 1),
                "Invested_Ratio_Pct": round(invested_fraction * 100, 1),
                "TCMB_Policy_Rate_Annual_Pct": round(rf_annual * 100, 2),
                "Cash_Repo_Return_Pct": round(rf_period * 100, 4),
                "Equity_Return_Pct": round(avg_equity_return * 100, 2),
                "Total_Weekly_Return_Pct": round(portfolio_return * 100, 2),
                "BIST100_NAV": round(bist100_nav, 2),
                "Exit_Event_Count": exit_event_count,
                "Stop_Loss_Count": stop_loss_count,
                "Take_Profit_Count": take_profit_count,
                "New_Buys": new_buy_count,
                "Position_Count": len(new_picks),
                "Not_Entered": not_entered_count,
                "DcfOvervalued_Count": dcf_overvalued_selected_count,
                "DcfOvervalued_Return_Count": dcf_overvalued_valid_count,
                "DcfOvervalued_Avg_Return_Pct": _avg_pct(
                    dcf_overvalued_return_sum,
                    dcf_overvalued_valid_count,
                ),
                "DcfOvervalued_Equity_Contribution_Pct": round(
                    (
                        dcf_overvalued_return_sum / n_valid
                        if n_valid > 0
                        else 0.0
                    ) * 100.0,
                    4,
                ),
                "DcfOther_Count": dcf_other_selected_count,
                "DcfOther_Return_Count": dcf_other_valid_count,
                "DcfOther_Avg_Return_Pct": _avg_pct(
                    dcf_other_return_sum,
                    dcf_other_valid_count,
                ),
                "DcfOther_Equity_Contribution_Pct": round(
                    (
                        dcf_other_return_sum / n_valid
                        if n_valid > 0
                        else 0.0
                    ) * 100.0,
                    4,
                ),
                "DcfOvervalued_Tickers": ",".join(dcf_overvalued_tickers),
                "Selected_Stocks_Details": " | ".join(stock_details)
            })

            # E. Update current portfolio list for next loop iteration
            prev_tickers_for_print = prev_tickers if i > 0 else set()
            prev_tickers = new_tickers
            portfolio = [{"company_id": p["company_id"], "ticker": p["ticker"]} for p in new_picks]
            # Names sold at this rebalance lose their trailing state — a
            # future re-entry starts fresh at the new entry price.
            for stale in [t for t in held_state if t not in new_tickers]:
                held_state.pop(stale, None)

            # F. Store cumulative results
            results.append({
                "date": next_d.isoformat(),
                "strategy_return": strategy_nav,
                "benchmark_return": bist100_nav,
                "alpha": strategy_nav - bist100_nav,
                **self._deflator_snapshot(next_d),
            })
            if persist_model_performance:
                self._save_performance_row(next_d.isoformat(), strategy_nav, bist100_nav)
                self.session.commit()  # Commit each week to protect against cash-signal rollbacks

            if console:
                n_new = len(new_tickers - prev_tickers_for_print)
                console.print(
                    f"  NAV={strategy_nav:.2f} | BIST={bist100_nav:.2f} | "
                    f"Cash={cash_pct*100:.0f}% | New buys={n_new}/{len(new_picks)} | "
                    f"Holdings={','.join(new_tickers)}"
                )

        if export_csv:
            # Production defaults to data/output; tests and research harnesses
            # can supply an isolated directory and must never overwrite the
            # canonical investor audit artifact.
            export_path = (
                Path(export_dir)
                if export_dir is not None
                else Path(__file__).resolve().parent.parent.parent / "data" / "output"
            )
            export_path.mkdir(parents=True, exist_ok=True)
            safe_strategy = strategy_variant.replace("-", "_")
            suffix = f"_{output_suffix}" if output_suffix else ""
            csv_file = export_path / f"backtest_weekly_details_{safe_strategy}{suffix}.csv"
            df_details = pd.DataFrame(weekly_details)
            df_details.to_csv(csv_file, index=False, encoding="utf-8-sig")
            events_file = export_path / f"backtest_exit_events_{safe_strategy}{suffix}.csv"
            event_columns = [
                "Start_Date",
                "End_Date",
                "Entry_Date",
                "Exit_Date",
                "Ticker",
                "Reason",
                "Entry_Price",
                "Exit_Price",
                "Stop_Loss",
                "Take_Profit",
                "Net_Return_Pct",
                "Was_New_Buy",
                "Strategy",
            ]
            pd.DataFrame(exit_events, columns=event_columns).to_csv(
                events_file,
                index=False,
                encoding="utf-8-sig",
            )
            if console:
                console.print(f"[bold green]Detailed week-by-week CSV exported to {csv_file}[/bold green]")
                console.print(f"[bold green]Exit events CSV exported to {events_file}[/bold green]")

        # A4/#9: run-level realism meta so the CLI/report can surface capacity
        # and the cost/idle assumptions that produced these numbers.
        self._last_run_meta = {
            "idle_cash_yield": idle_mode,
            "extra_cost_round_trip_pct": round(cost_round_trip * 100.0, 4),
            "base_friction_round_trip_pct": round(applied_friction * 100.0, 4),
            "liquidity_impact_enabled": impact_enabled,
            "capacity_try": (min(capacity_samples) if capacity_samples else None),
            "capacity_portfolio_try": (
                self._bt_cfg["impact_portfolio_try"] if impact_enabled else None
            ),
            "scoring_pipeline_version": SCORING_PIPELINE_VERSION,
            "stale_score_cache_dates": stale_score_cache_dates,
            "rebalance_anchor_date": (
                rebalance_anchor_date.isoformat() if rebalance_anchor_date else None
            ),
            "signal_contract": (
                "T-1 completed data -> T first executable open"
                if execution_mode == self._EXECUTION_SAME_DAY_OPEN
                else "signal date -> later execution"
            ),
        }

        frame = pd.DataFrame(results)

        if persist_model_performance:
            # Daily-run artifact for the PWA real-returns card (L1). The
            # incremental daily run appends only a point or two, so the
            # run-local frame is degenerate (the 2026-07-10 manifest shipped
            # a one-day window of zeros) — summarize the FULL stored series
            # instead. A summary/IO problem must never fail the backtest.
            try:
                full_frame = self._full_performance_frame()
                if len(full_frame) >= 10:
                    persist_daily_summary(
                        self._summarize_performance(full_frame),
                        start_date=date.fromisoformat(str(full_frame["date"].iloc[0])[:10]),
                        end_date=date.fromisoformat(str(full_frame["date"].iloc[-1])[:10]),
                    )
            except Exception as exc:
                logger.warning("Daily summary persist failed: %s", exc)

        return frame

    def _full_performance_frame(self) -> pd.DataFrame:
        """Complete stored NAV series + deflator snapshots, for summaries.

        model_performance is the source of truth across incremental runs;
        deflator levels are re-attached per date from the run's caches
        (built earlier in run_1y_backtest), so the deflated blocks cover the
        whole history rather than just the freshly appended points.
        """
        rows = (
            self.session.query(ModelPerformance)
            .order_by(ModelPerformance.date.asc())
            .all()
        )
        records = []
        for row in rows:
            row_date = row.date
            if isinstance(row_date, str):
                row_date = date.fromisoformat(row_date[:10])
            records.append(
                {
                    "date": row_date.isoformat(),
                    "strategy_return": row.strategy_return,
                    "benchmark_return": row.benchmark_return,
                    **self._deflator_snapshot(row_date),
                }
            )
        return pd.DataFrame(records)

    def _deflator_history_start(
        self,
        requested_start: date,
        *,
        persist_model_performance: bool,
    ) -> date:
        """Align FX/gold history with the full NAV used by daily summaries."""

        if not persist_model_performance:
            return requested_start
        earliest = self.session.query(func.min(ModelPerformance.date)).scalar()
        if earliest is None:
            return requested_start
        try:
            earliest_date = (
                earliest
                if isinstance(earliest, date)
                else date.fromisoformat(str(earliest)[:10])
            )
        except (TypeError, ValueError):
            return requested_start
        return min(requested_start, earliest_date)

    def run_investor_grade_suite(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        console=None,
        *,
        strategy_variant: str = "index_aware",
        execution_mode: str = _EXECUTION_NEXT_OPEN,
        stress_frictions: Optional[list[float]] = None,
        persist_base: bool = True,
    ) -> dict[str, Any]:
        """Run the main backtest plus slippage stress cases and audit checks.

        The base case is the one intended for publication. Stress cases do not
        write to ``model_performance``; they exist to answer "does this still
        work if fills are worse than expected?"
        """
        execution_mode = self._normalize_execution_mode(execution_mode)
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(weeks=208)
            while start_date.weekday() != 0:
                start_date += timedelta(days=1)

        friction_cases = [self._FRICTION_ROUND_TRIP]
        friction_cases.extend(stress_frictions or [0.0075, 0.0100, 0.0150])

        # The published investor-grade audit must simulate the same calendar
        # and stop policy as live selection, including rotation parity.
        from us_picker.portfolio.exit_rules import _load_trailing_config
        from us_picker.portfolio.rotation import load_rotation_config

        rotation_weeks, rotation_anchor = load_rotation_config()
        trailing_cfg = _load_trailing_config()

        cases: list[dict[str, Any]] = []
        for index, friction in enumerate(friction_cases):
            label = "base" if index == 0 else f"stress_{friction * 100:.2f}pct"
            if console:
                console.print(f"[cyan]Investor-grade case: {label}[/cyan]")
            df = self.run_1y_backtest(
                start_date=start_date,
                end_date=end_date,
                console=console,
                strategy_variant=strategy_variant,
                execution_mode=execution_mode,
                friction_round_trip=friction,
                persist_model_performance=(persist_base and index == 0),
                export_csv=(index == 0),
                output_suffix=None if index == 0 else label,
                rebalance_every_n_weeks=rotation_weeks,
                rebalance_anchor_date=rotation_anchor,
                intra_window_trailing=trailing_cfg["enabled"],
                trail_min_pct=trailing_cfg["min_pct"],
                trail_max_pct=trailing_cfg["max_pct"],
            )
            summary = self._summarize_performance(df)
            summary.update({
                "case": label,
                "friction_round_trip_pct": round(friction * 100.0, 4),
            })
            cases.append(summary)

        price_jump_audit = self.audit_price_jumps(start_date, end_date)
        survivorship_audit = self.audit_survivorship_risk(start_date, end_date)
        report = {
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_variant": strategy_variant.replace("-", "_"),
            "execution_mode": execution_mode,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "rebalance_every_n_weeks": rotation_weeks,
            "rebalance_anchor_date": (
                rotation_anchor.isoformat() if rotation_anchor else None
            ),
            "intra_window_trailing": trailing_cfg["enabled"],
            "trail_min_pct": trailing_cfg["min_pct"],
            "trail_max_pct": trailing_cfg["max_pct"],
            "cases": cases,
            "price_jump_audit": price_jump_audit,
            "survivorship_audit": survivorship_audit,
            "gates": self._investor_grade_gates(
                cases,
                price_jump_audit,
                survivorship_audit,
                execution_mode=execution_mode,
            ),
        }

        export_path = Path(__file__).resolve().parent.parent.parent / "data" / "output"
        export_path.mkdir(parents=True, exist_ok=True)
        safe_strategy = strategy_variant.replace("-", "_")
        report_path = export_path / f"backtest_investor_grade_{safe_strategy}.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        if console:
            console.print(f"[bold green]Investor-grade report exported to {report_path}[/bold green]")
        return report

    def audit_price_jumps(
        self,
        start_date: date,
        end_date: date,
        *,
        ratio_threshold: float = 5.0,
        sample_limit: int = 25,
    ) -> dict[str, Any]:
        """Flag suspicious adjusted-price jumps in non-index stocks."""
        rows = (
            self.session.query(
                Company.ticker,
                DailyPrice.date,
                DailyPrice.adjusted_close,
                DailyPrice.close,
            )
            .join(Company, Company.id == DailyPrice.company_id)
            .filter(DailyPrice.date >= start_date, DailyPrice.date <= end_date)
            .filter(or_(Company.company_type.is_(None), Company.company_type != "INDEX"))
            .order_by(Company.ticker.asc(), DailyPrice.date.asc())
            .all()
        )

        lower_threshold = 1.0 / ratio_threshold
        previous_by_ticker: dict[str, tuple[date, float]] = {}
        flags: list[dict[str, Any]] = []
        scanned_rows = 0

        for ticker, row_date, adjusted_close, close in rows:
            price = adjusted_close if adjusted_close is not None and adjusted_close > 0 else close
            if price is None or price <= 0:
                continue
            scanned_rows += 1
            price = float(price) * get_price_splice_factor_by_ticker(
                self.session,
                ticker,
                row_date,
            )
            previous = previous_by_ticker.get(ticker)
            if previous is not None:
                prev_date, prev_price = previous
                if prev_price > 0:
                    ratio = price / prev_price
                    if ratio >= ratio_threshold or ratio <= lower_threshold:
                        flags.append({
                            "ticker": ticker,
                            "previous_date": prev_date.isoformat(),
                            "date": row_date.isoformat(),
                            "previous_price": round(prev_price, 6),
                            "price": round(price, 6),
                            "ratio": round(ratio, 6),
                        })
            previous_by_ticker[ticker] = (row_date, price)

        return {
            "ratio_threshold": ratio_threshold,
            "scanned_price_rows": scanned_rows,
            "flag_count": len(flags),
            "sample": flags[:sample_limit],
        }

    def audit_survivorship_risk(
        self,
        start_date: date,
        end_date: date,
        *,
        sample_limit: int = 25,
    ) -> dict[str, Any]:
        """Report inactive non-index companies with price history in the test window."""
        rows = (
            self.session.query(
                Company.ticker,
                Company.delisting_date,
                func.min(DailyPrice.date).label("first_price_date"),
                func.max(DailyPrice.date).label("last_price_date"),
                func.count(DailyPrice.id).label("price_rows"),
            )
            .join(DailyPrice, DailyPrice.company_id == Company.id)
            .filter(DailyPrice.date >= start_date, DailyPrice.date <= end_date)
            .filter(Company.is_active.is_(False))
            .filter(or_(Company.company_type.is_(None), Company.company_type != "INDEX"))
            .filter(~Company.ticker.ilike("MOCK%"))
            .filter(~Company.ticker.ilike("TEST%"))
            .group_by(Company.id)
            .order_by(Company.ticker.asc())
            .all()
        )
        sample = [
            {
                "ticker": row.ticker,
                "delisting_date": row.delisting_date.isoformat() if row.delisting_date else None,
                "first_price_date": row.first_price_date.isoformat() if row.first_price_date else None,
                "last_price_date": row.last_price_date.isoformat() if row.last_price_date else None,
                "price_rows": int(row.price_rows or 0),
            }
            for row in rows[:sample_limit]
        ]
        return {
            "inactive_priced_company_count": len(rows),
            "sample": sample,
            "status": "pass" if len(rows) == 0 else "warn",
        }

    @classmethod
    def _normalize_execution_mode(cls, execution_mode: str) -> str:
        normalized = (execution_mode or cls._EXECUTION_NEXT_OPEN).strip().lower()
        normalized = normalized.replace("-", "_")
        if normalized not in cls._VALID_EXECUTION_MODES:
            raise ValueError(
                f"Unknown execution_mode {execution_mode!r}. "
                f"Valid: {sorted(cls._VALID_EXECUTION_MODES)}"
            )
        return normalized

    @staticmethod
    def _adjusted_price_from_bar(
        raw_value: Optional[float],
        raw_close: Optional[float],
        adjusted_close: Optional[float],
    ) -> Optional[float]:
        if raw_value is None or raw_value <= 0:
            return None
        if raw_close is not None and raw_close > 0 and adjusted_close is not None and adjusted_close > 0:
            return float(raw_value) * (float(adjusted_close) / float(raw_close))
        return float(raw_value)

    def _get_entry_price(
        self,
        ticker: str,
        signal_date: date,
        next_rebalance_date: date,
        execution_mode: str,
    ) -> tuple[float, date]:
        """Return a legal simulated fill for the selected execution contract.

        ``same_day_open`` treats ``signal_date`` as the effective trading
        session and fills on that session's first available open. ``next_open``
        starts strictly after the signal session. A missing bar returns
        ``(0.0, signal_date)`` so the caller leaves that slot in cash.
        """
        execution_mode = self._normalize_execution_mode(execution_mode)
        if execution_mode == self._EXECUTION_SAME_DAY_CLOSE:
            return self._get_price(ticker, signal_date), signal_date

        date_operator = (
            DailyPrice.date >= signal_date
            if execution_mode == self._EXECUTION_SAME_DAY_OPEN
            else DailyPrice.date > signal_date
        )
        row = (
            self.session.query(
                DailyPrice.date,
                DailyPrice.open,
                DailyPrice.close,
                DailyPrice.adjusted_close,
            )
            .join(Company, Company.id == DailyPrice.company_id)
            .filter(
                Company.ticker == ticker,
                date_operator,
                DailyPrice.date <= next_rebalance_date,
            )
            .order_by(DailyPrice.date.asc())
            .first()
        )
        if row is None:
            return 0.0, signal_date

        row_date, raw_open, raw_close, adjusted_close = row
        open_price = self._adjusted_price_from_bar(raw_open, raw_close, adjusted_close)
        close_price = (
            float(adjusted_close)
            if adjusted_close is not None and adjusted_close > 0
            else self._adjusted_price_from_bar(raw_close, raw_close, adjusted_close)
        )
        price = open_price if open_price is not None and open_price > 0 else close_price
        if price is None or price <= 0:
            return 0.0, signal_date
        splice_factor = get_price_splice_factor_by_ticker(self.session, ticker, row_date)
        return float(price) * splice_factor, row_date

    @staticmethod
    def _nav_metrics(nav: pd.Series, years: float) -> dict[str, Optional[float]]:
        """Total return, CAGR, max drawdown and a zero-rate Sharpe for a NAV.

        Sharpe here is the annualized mean/stdev of periodic NAV returns with
        NO risk-free subtraction — intentionally simple. On NOMINAL TRY it
        flatters (the whole point of A1); the gram-gold / USD / CPI-real
        variants are the honest, comparable risk-adjusted numbers.
        """
        nav = pd.to_numeric(nav, errors="coerce").dropna()
        blank = {
            "total_return_pct": None,
            "cagr_pct": None,
            "max_drawdown_pct": None,
            "sharpe": None,
        }
        if len(nav) < 2 or float(nav.iloc[0]) <= 0:
            return blank
        growth = float(nav.iloc[-1]) / float(nav.iloc[0])
        total = growth - 1.0
        cagr = (growth ** (1.0 / years) - 1.0) if years and years > 0 else None
        running_max = nav.cummax()
        drawdown = float((nav / running_max - 1.0).min())
        rets = nav.pct_change().dropna()
        periods_per_year = (len(nav) - 1) / years if years and years > 0 else 0.0
        std = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
        sharpe = (
            (float(rets.mean()) / std) * (periods_per_year ** 0.5)
            if std > 0 and periods_per_year > 0
            else None
        )
        return {
            "total_return_pct": round(total * 100.0, 4),
            "cagr_pct": round(cagr * 100.0, 4) if cagr is not None else None,
            "max_drawdown_pct": round(drawdown * 100.0, 4),
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
        }

    @staticmethod
    def _tail_risk_metrics(
        periodic_returns: pd.Series,
        drawdown: pd.Series,
        cvar_alpha: float = 0.05,
    ) -> dict[str, Optional[float]]:
        """Distribution/tail-risk stats a nominal Sharpe hides (B1).

        - skew / excess-kurtosis of periodic returns (fat left tail warning)
        - downside deviation (std of negative periods only)
        - CVaR: mean of the worst ``cvar_alpha`` fraction of periods
        - Ulcer index: RMS of the drawdown path (pain, not just the max point)
        - worst single period
        All in percent where applicable.
        """
        rets = pd.to_numeric(periodic_returns, errors="coerce").dropna()
        blank = {
            "skew": None,
            "excess_kurtosis": None,
            "downside_deviation_pct": None,
            "cvar_5pct": None,
            "ulcer_index_pct": None,
            "worst_period_pct": None,
        }
        if len(rets) < 3:
            return blank
        negatives = rets[rets < 0]
        n_tail = max(1, int(len(rets) * cvar_alpha))
        worst_tail = rets.nsmallest(n_tail)
        dd = pd.to_numeric(drawdown, errors="coerce").dropna()
        ulcer = float(((dd * 100.0) ** 2).mean() ** 0.5) if len(dd) else None
        return {
            "skew": round(float(rets.skew()), 4),
            "excess_kurtosis": round(float(rets.kurt()), 4),
            "downside_deviation_pct": (
                round(float(negatives.std(ddof=1)) * 100.0, 4) if len(negatives) > 1 else None
            ),
            "cvar_5pct": round(float(worst_tail.mean()) * 100.0, 4),
            "ulcer_index_pct": round(ulcer, 4) if ulcer is not None else None,
            "worst_period_pct": round(float(rets.min()) * 100.0, 4),
        }

    @staticmethod
    def _summarize_performance(df: pd.DataFrame) -> dict[str, Any]:
        """Return compact performance/risk metrics for a NAV dataframe."""
        if df.empty:
            return {
                "points": 0,
                "total_return_pct": None,
                "benchmark_return_pct": None,
                "alpha_pct": None,
                "max_drawdown_pct": None,
                "max_weekly_return_pct": None,
                "min_weekly_return_pct": None,
                "rolling_52w_windows": 0,
                "rolling_52w_positive_rate_pct": None,
                "rolling_52w_beat_benchmark_rate_pct": None,
                "rolling_52w_min_return_pct": None,
                "rolling_52w_min_alpha_pct": None,
            }

        frame = df.copy()
        frame["strategy_return"] = pd.to_numeric(frame["strategy_return"], errors="coerce")
        frame["benchmark_return"] = pd.to_numeric(frame["benchmark_return"], errors="coerce")
        frame = frame.dropna(subset=["strategy_return", "benchmark_return"])
        if frame.empty:
            return {"points": 0}

        strategy = frame["strategy_return"]
        benchmark = frame["benchmark_return"]
        weekly = strategy.pct_change().dropna()
        running_max = strategy.cummax()
        drawdown = (strategy / running_max) - 1.0

        rolling_returns: list[float] = []
        rolling_alphas: list[float] = []
        for idx in range(52, len(frame)):
            start_strategy = strategy.iloc[idx - 52]
            start_benchmark = benchmark.iloc[idx - 52]
            if start_strategy <= 0 or start_benchmark <= 0:
                continue
            strat_ret = strategy.iloc[idx] / start_strategy - 1.0
            bench_ret = benchmark.iloc[idx] / start_benchmark - 1.0
            rolling_returns.append(strat_ret)
            rolling_alphas.append(strat_ret - bench_ret)

        def pct(value: float | None) -> float | None:
            return round(value * 100.0, 4) if value is not None else None

        rolling_count = len(rolling_returns)
        strategy_total_pct = (float(strategy.iloc[-1]) / float(strategy.iloc[0]) - 1.0) * 100.0
        benchmark_total_pct = (float(benchmark.iloc[-1]) / float(benchmark.iloc[0]) - 1.0) * 100.0

        # A1: elapsed years (for CAGR / annualized Sharpe) from the date column.
        years = 0.0
        if "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
            if len(dates) >= 2:
                years = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
        nominal_metrics = BacktestEngine._nav_metrics(strategy, years)
        tail_metrics = BacktestEngine._tail_risk_metrics(weekly, drawdown)

        # A1: report the strategy NAV in real (CPI) and hard-currency terms.
        # Nominal-TRY headline returns overstate the edge across a hyperinflation
        # window; dividing the NAV by each deflator gives the purchasing-power /
        # gram-gold / USD picture. Benchmark is deflated the same way so alpha
        # stays meaningful in that unit.
        deflated: dict[str, Any] = {}
        for unit_key, column in (
            ("gram_gold", "gram_gold_try"),
            ("usd", "usd_try"),
            ("cpi_real", "cpi_index"),
        ):
            if column not in frame.columns:
                continue
            deflator = pd.to_numeric(frame[column], errors="coerce")
            if deflator.notna().sum() < 2 or not bool((deflator.dropna() > 0).all()):
                continue
            unit_metrics = BacktestEngine._nav_metrics(strategy / deflator, years)
            bench_metrics = BacktestEngine._nav_metrics(benchmark / deflator, years)
            unit_metrics["benchmark_total_return_pct"] = bench_metrics["total_return_pct"]
            if (
                unit_metrics["total_return_pct"] is not None
                and bench_metrics["total_return_pct"] is not None
            ):
                unit_metrics["alpha_pct"] = round(
                    unit_metrics["total_return_pct"] - bench_metrics["total_return_pct"], 4
                )
            else:
                unit_metrics["alpha_pct"] = None
            deflated[unit_key] = unit_metrics

        return {
            "points": int(len(frame)),
            "years": round(years, 4) if years else None,
            "cagr_pct": nominal_metrics["cagr_pct"],
            "sharpe_nominal": nominal_metrics["sharpe"],
            "deflated": deflated,
            "tail_risk": tail_metrics,
            "total_return_pct": round(strategy_total_pct, 4),
            "benchmark_return_pct": round(benchmark_total_pct, 4),
            "alpha_pct": round(strategy_total_pct - benchmark_total_pct, 4),
            "max_drawdown_pct": pct(float(drawdown.min())),
            "max_weekly_return_pct": pct(float(weekly.max())) if not weekly.empty else None,
            "min_weekly_return_pct": pct(float(weekly.min())) if not weekly.empty else None,
            "rolling_52w_windows": rolling_count,
            "rolling_52w_positive_rate_pct": (
                round(sum(1 for value in rolling_returns if value > 0) / rolling_count * 100.0, 4)
                if rolling_count
                else None
            ),
            "rolling_52w_beat_benchmark_rate_pct": (
                round(sum(1 for value in rolling_alphas if value > 0) / rolling_count * 100.0, 4)
                if rolling_count
                else None
            ),
            "rolling_52w_min_return_pct": pct(min(rolling_returns)) if rolling_returns else None,
            "rolling_52w_min_alpha_pct": pct(min(rolling_alphas)) if rolling_alphas else None,
        }

    @staticmethod
    def _investor_grade_gates(
        cases: list[dict[str, Any]],
        price_jump_audit: dict[str, Any],
        survivorship_audit: Optional[dict[str, Any]] = None,
        *,
        execution_mode: str = _EXECUTION_NEXT_OPEN,
    ) -> list[dict[str, Any]]:
        """Return pass/warn checks used by the PWA confidence panel."""
        base = cases[0] if cases else {}
        worst_stress = cases[-1] if cases else {}
        same_day_open = execution_mode == BacktestEngine._EXECUTION_SAME_DAY_OPEN
        gates = [
            {
                "key": "execution",
                "label": (
                    "T-1 completed data -> T opening execution"
                    if same_day_open
                    else "T+1 next-open execution"
                ),
                "status": "pass",
                "detail": (
                    "Signals use the prior completed session and fill at the "
                    "effective session's first available open."
                    if same_day_open
                    else "Signals are tested with next available open fills."
                ),
            },
            {
                "key": "slippage",
                "label": "Slippage stress",
                "status": (
                    "pass"
                    if (worst_stress.get("alpha_pct") is not None and worst_stress.get("alpha_pct") > 0)
                    else "warn"
                ),
                "detail": (
                    f"Worst stress alpha: {worst_stress.get('alpha_pct')}%"
                    if worst_stress
                    else "No stress case available."
                ),
            },
            {
                "key": "drawdown",
                "label": "Drawdown sanity",
                "status": (
                    "pass"
                    if (
                        base.get("max_drawdown_pct") is not None
                        and base.get("max_drawdown_pct") >= -35.0
                    )
                    else "warn"
                ),
                "detail": f"Max drawdown: {base.get('max_drawdown_pct')}%",
            },
            {
                "key": "price_jumps",
                "label": "Price jump audit",
                "status": "pass" if price_jump_audit.get("flag_count", 0) == 0 else "warn",
                "detail": f"{price_jump_audit.get('flag_count', 0)} suspicious jumps flagged.",
            },
            {
                "key": "survivorship",
                "label": "As-of universe",
                "status": (survivorship_audit or {}).get("status", "warn"),
                "detail": (
                    "No inactive priced companies found in the tested window."
                    if (survivorship_audit or {}).get("inactive_priced_company_count") == 0
                    else (
                        f"{(survivorship_audit or {}).get('inactive_priced_company_count', 'Unknown')} "
                        "inactive priced companies need delisted-name coverage."
                    )
                ),
            },
        ]
        return gates

    def _get_cash_pct(self, scoring_date: date) -> float:
        """Evaluate the cash-signal state machine for the given date.

        Returns the cash percentage (0.0 to 0.75) that should be held in
        cash according to the market/macro regime at *scoring_date*.

        Falls back to 0.0 (fully invested) if the signal cannot be computed
        (e.g. insufficient macro data for early backtest dates).

        Uses a savepoint to isolate the cash signal computation from the
        main backtest session state, preventing rollbacks from wiping out
        previously committed performance rows.
        """
        try:
            from us_picker.portfolio.cash_signal import CashSignalCalculator
            # Use a savepoint (nested transaction) so any internal rollback
            # only affects the savepoint, not the outer transaction.
            nested = self.session.begin_nested()
            try:
                calculator = CashSignalCalculator()
                result = calculator.compute(
                    self.session,
                    scoring_date,
                    persist=False,
                )
                nested.commit()
                return result.cash_pct
            except Exception:
                nested.rollback()
                return 0.0
        except Exception as exc:
            logger.debug("Cash signal unavailable for %s: %s", scoring_date, exc)
            return 0.0

    def _get_risk_free_rate(self, target_date: date) -> float:
        """Get the annual risk-free rate (policy rate) for the given date."""
        from us_picker.db.schema import MacroRegime
        row = (
            self.session.query(MacroRegime.policy_rate_pct)
            .filter(MacroRegime.date <= target_date)
            .filter(MacroRegime.policy_rate_pct.isnot(None))
            .order_by(desc(MacroRegime.date))
            .first()
        )
        # Fallback to 40% (0.40) if no data is found (conservative for TR recent history)
        return float(row[0]) if row and row[0] is not None and row[0] > 0 else 0.40

    def _get_weekly_exit(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
        stop_loss: Optional[float],
        take_profit: Optional[float] = None,
        *,
        include_start_date: bool = False,
        trail_state: Optional[dict] = None,
    ) -> tuple[float, bool, Optional[str], Optional[date]]:
        """Check intraperiod OHLC prices for stop-loss / take-profit hits.

        Returns (exit_price, exited, exit_reason, exit_date). For same-day
        ranges that hit both stop and target, stop-loss wins as a conservative
        long-side assumption.

        trail_state (B1 continuity): mutable dict ``{"pct", "high", "stop"}``.
        When given, each bar is first evaluated against the CURRENT stop, then
        the stop ratchets to ``max(stop, highest_close × (1 − pct))`` — same
        end-of-day semantics as the live check-exits updater. The dict is
        updated in place so the caller can carry the state into the next
        rebalance period.
        """
        from us_picker.db.schema import DailyPrice, Company
        date_filter = (
            DailyPrice.date >= start_date
            if include_start_date
            else DailyPrice.date > start_date
        )
        rows = (
            self.session.query(
                DailyPrice.date,
                DailyPrice.open,
                DailyPrice.high,
                DailyPrice.low,
                DailyPrice.close,
                DailyPrice.adjusted_close,
            )
            .join(Company, Company.id == DailyPrice.company_id)
            .filter(Company.ticker == ticker, date_filter, DailyPrice.date <= end_date)
            .order_by(DailyPrice.date.asc())
            .all()
        )

        valid_take = take_profit is not None and take_profit > 0
        last_price: Optional[float] = None
        trailing = trail_state is not None

        for row in rows:
            valid_stop = stop_loss is not None and stop_loss > 0
            row_date, raw_open, raw_high, raw_low, raw_close, adj_close = row
            close_price = (
                float(adj_close)
                if adj_close is not None and adj_close > 0
                else self._adjusted_price_from_bar(raw_close, raw_close, adj_close)
            )
            open_price = self._adjusted_price_from_bar(raw_open, raw_close, adj_close)
            high_price = self._adjusted_price_from_bar(raw_high, raw_close, adj_close)
            low_price = self._adjusted_price_from_bar(raw_low, raw_close, adj_close)
            splice_factor = get_price_splice_factor_by_ticker(self.session, ticker, row_date)
            close_price = close_price * splice_factor if close_price is not None else None
            open_price = open_price * splice_factor if open_price is not None else None
            high_price = high_price * splice_factor if high_price is not None else None
            low_price = low_price * splice_factor if low_price is not None else None

            available_prices = [
                p for p in (open_price, high_price, low_price, close_price)
                if p is not None
            ]
            if not available_prices:
                continue

            if open_price is None:
                open_price = close_price if close_price is not None else available_prices[0]
            if high_price is None:
                high_price = max(available_prices)
            if low_price is None:
                low_price = min(available_prices)
            if close_price is None:
                close_price = available_prices[-1]

            last_price = close_price

            if valid_stop and open_price <= stop_loss:
                return (open_price, True, "STOP_LOSS", row_date)
            if valid_take and open_price >= take_profit:
                return (float(take_profit), True, "TAKE_PROFIT", row_date)
            if (
                valid_stop
                and valid_take
                and low_price <= stop_loss
                and high_price >= take_profit
            ):
                return (float(stop_loss), True, "STOP_LOSS", row_date)
            if valid_stop and low_price <= stop_loss:
                return (float(stop_loss), True, "STOP_LOSS", row_date)
            if valid_take and high_price >= take_profit:
                return (float(take_profit), True, "TAKE_PROFIT", row_date)

            # B1: end-of-day trailing ratchet — mirrors the live daily
            # check-exits pass. Applied only after today's bar survived the
            # existing stop, and the stop never moves down.
            if trailing and close_price is not None:
                high_water = max(trail_state.get("high") or close_price, close_price)
                trail_state["high"] = high_water
                candidate = high_water * (1.0 - float(trail_state.get("pct") or 0.25))
                if candidate > (stop_loss or 0.0):
                    stop_loss = candidate
                    trail_state["stop"] = candidate

        # If we didn't stop out or take profit, return the last available price in that window
        if last_price is not None:
            return (last_price, False, None, None)

        # No print at all inside the window: suspension/delisting risk.
        # Force a conservative exit at the last known price with a 20%
        # haircut instead of pretending a flat week at a phantom price
        # (B3 execution realism, item 4).
        stale_price = self._get_price(ticker, end_date)
        if stale_price > 0:
            return (stale_price * 0.80, True, "NO_PRICE_FORCED_EXIT", None)
        return (0.0, False, None, None)

    def _get_price(self, ticker: str, target_date: date) -> float:
        """Return latest adjusted close when present, else plain close on or before target_date."""
        return get_spliced_price_by_ticker(self.session, ticker, target_date)

    @staticmethod
    def _asof_series_value(series, target_date: date) -> float:
        """As-of lookup: newest series value on or before target_date.

        Returns the first value when target_date predates the series (avoids
        a divide-by-zero downstream) and 0.0 when the series is missing/empty.
        """
        if series is None or getattr(series, "empty", True):
            return 0.0
        target_ts = pd.Timestamp(target_date)
        available = series.index[series.index <= target_ts]
        if len(available) == 0:
            first = series.iloc[0]
            return float(first.item() if hasattr(first, "item") else first)
        val = series.loc[available[-1]]
        return float(val.item() if hasattr(val, "item") else val)

    def _get_gram_gold_price(self, target_date: date) -> float:
        """Return the closest available gram gold price from the cache."""
        return self._asof_series_value(self._gold_cache, target_date)

    def _get_usd_try(self, target_date: date) -> float:
        """Return the closest available USD/TRY rate from the cache (A1)."""
        return self._asof_series_value(self._usd_cache, target_date)

    def _get_cpi_index(self, target_date: date) -> float:
        """Return the closest available CPI index level from the cache (A1)."""
        return self._asof_series_value(self._cpi_cache, target_date)

    def _idle_yield(self, mode: str, start: date, end: date) -> float:
        """Return earned by idle capital over (start, end] (#9).

        ``gram_gold`` uses the gram-gold move; ``repo`` (and gram-gold's
        fallback) uses the TCMB policy rate pro-rated by calendar days;
        ``none`` earns 0% (current behavior).
        """
        if not mode or mode == "none" or end <= start:
            return 0.0
        if mode == "gram_gold":
            g1 = self._get_gram_gold_price(start)
            g2 = self._get_gram_gold_price(end)
            if g1 > 0 and g2 > 0:
                return (g2 / g1) - 1.0
        # repo, or gram-gold fallback when gold data is missing
        annual = self._get_risk_free_rate(start)
        days = (end - start).days
        return (1.0 + annual) ** (days / 365.25) - 1.0

    def _get_avg_turnover_try(
        self, company_id: int, as_of: date, lookback_days: int
    ) -> float:
        """Average daily TRY turnover over the lookback window (A4 capacity).

        IsYatirim rows already store TRY turnover in ``volume``-derived fields;
        we approximate with ``close × volume`` which is TRY for both sources.
        Returns 0.0 when there is no usable data.
        """
        cutoff = as_of - timedelta(days=lookback_days * 2)  # calendar margin
        rows = (
            self.session.query(DailyPrice.close, DailyPrice.volume)
            .filter(
                DailyPrice.company_id == company_id,
                DailyPrice.date > cutoff,
                DailyPrice.date <= as_of,
                DailyPrice.close.isnot(None),
                DailyPrice.volume.isnot(None),
            )
            .order_by(DailyPrice.date.desc())
            .limit(lookback_days)
            .all()
        )
        turnovers = [
            float(c) * float(v)
            for c, v in rows
            if c and v and float(c) > 0 and float(v) > 0
        ]
        if not turnovers:
            return 0.0
        return sum(turnovers) / len(turnovers)

    def _deflator_snapshot(self, target_date: date) -> dict[str, Optional[float]]:
        """Deflator levels at target_date for real/hard-currency NAV reporting.

        0.0 (series missing) is mapped to None so _summarize_performance can
        skip that unit cleanly instead of dividing by zero.
        """
        def _clean(value: float) -> Optional[float]:
            return value if value and value > 0 else None

        return {
            "gram_gold_try": _clean(self._get_gram_gold_price(target_date)),
            "usd_try": _clean(self._get_usd_try(target_date)),
            "cpi_index": _clean(self._get_cpi_index(target_date)),
        }

    def _load_cpi_cache(self):
        """Load the TCMB CPI index series (as-of deflator) once per run.

        Uses the ``cpi_history`` table (monthly index levels). The real-return
        column is only as trustworthy as this series' base-year continuity;
        a stitched/rebased series would distort CPI-real numbers (see the
        TP.FG.J0 → TP.TUKFIY2025 base change caveat). Returns None on any
        problem so the backtest never fails because CPI is unavailable.
        """
        try:
            from us_picker.db.schema import CpiHistory

            rows = (
                self.session.query(CpiHistory.date, CpiHistory.cpi_index)
                .filter(CpiHistory.cpi_index.isnot(None))
                .order_by(CpiHistory.date.asc())
                .all()
            )
            if not rows:
                return None
            index = [pd.Timestamp(row[0]) for row in rows]
            values = [float(row[1]) for row in rows]
            series = pd.Series(values, index=index).dropna()
            series = series[series > 0]
            return series if not series.empty else None
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("CPI cache unavailable: %s", exc)
            return None

    def _save_performance_row(self, date_str: str, strategy: float, benchmark: float) -> None:
        """Upsert a row in the model_performance table."""
        alpha_val = strategy - benchmark
        existing = self.session.query(ModelPerformance).filter(ModelPerformance.date == date_str).first()
        if existing:
            existing.strategy_return = strategy
            existing.benchmark_return = benchmark
            existing.alpha = alpha_val
        else:
            self.session.add(ModelPerformance(
                date=date_str,
                strategy_return=strategy,
                benchmark_return=benchmark,
                alpha=alpha_val
            ))


def _build_rebalance_dates(
    start_date: date,
    end_date: date,
    every_n_weeks: int = 1,
    *,
    anchor_date: Optional[date] = None,
) -> list[date]:
    """Return Monday rebalance dates, keeping every Nth Monday from the start.

    ``every_n_weeks=1`` reproduces the historical weekly cadence exactly;
    2 = bi-weekly, 4 = ~monthly (C1 rotation-frequency calibration).
    """
    every_n_weeks = max(1, int(every_n_weeks))
    mondays: list[date] = []
    curr = start_date
    while curr.weekday() != 0:
        curr += timedelta(days=1)
    while curr <= end_date:
        mondays.append(curr)
        curr += timedelta(days=7)
    if every_n_weeks == 1 or anchor_date is None:
        return mondays[::every_n_weeks]

    anchor_monday = anchor_date - timedelta(days=anchor_date.weekday())
    return [
        monday
        for monday in mondays
        if ((monday - anchor_monday).days // 7) % every_n_weeks == 0
    ]


def _scoring_universe(session: Session, scoring_date: date) -> list[Company]:
    """Companies eligible for point-in-time scoring at *scoring_date*.

    Point-in-time membership (audit CRITICAL #4): companies that have since
    delisted must still be scored at historical dates, otherwise the
    backtest only ever sees today's survivors.

    INDEX/mock rows are ``is_active=0`` with no ``delisting_date``, so the
    as-of proxy would resurrect them at historical dates; a single
    model_type INDEX row aborts ``compose_all`` and leaves the whole date
    without composites (found on the first 2018 full-history run).
    """
    from us_picker.portfolio.universes import active_as_of_criterion

    return (
        session.query(Company)
        .filter(active_as_of_criterion(session, scoring_date))
        .filter(or_(Company.company_type.is_(None), Company.company_type != "INDEX"))
        .filter(~Company.ticker.ilike("MOCK%"))
        .filter(~Company.ticker.ilike("TEST%"))
        .all()
    )


def _ensure_scores_for_date(
    session: Session,
    scoring_date: date,
    console=None,
    *,
    rebuild_stale_scores: bool = False,
) -> bool:
    """Ensure point-in-time scoring results exist in the DB for the given date.
    
    If not, executes the full scoring, normalization, risk classification,
    red flag detection, and composition stages, saving results to the DB.
    """
    existing_query = session.query(ScoringResult).filter(
        ScoringResult.scoring_date == scoring_date
    )
    existing_count = existing_query.count()
    if existing_count > 0:
        current_count = existing_query.filter(
            ScoringResult.pipeline_version == SCORING_PIPELINE_VERSION
        ).count()
        if current_count == existing_count:
            if console:
                console.print(
                    f"[dim]  Using {SCORING_PIPELINE_VERSION} cached scores "
                    f"for {scoring_date}[/dim]"
                )
            return True

        if not rebuild_stale_scores:
            if console:
                console.print(
                    f"[bold yellow]  WARNING: {scoring_date} uses {existing_count} "
                    "legacy/stale cached scores. Re-run with "
                    "--rebuild-stale-scores before trusting model A/B.[/bold yellow]"
                )
            return False

        if console:
            console.print(
                f"[yellow]  Rebuilding {existing_count} stale score rows for "
                f"{scoring_date} as {SCORING_PIPELINE_VERSION}...[/yellow]"
            )
        existing_query.delete(synchronize_session=False)
        session.flush()

    if console:
        console.print(f"[yellow]  No scores cached for {scoring_date}. Computing PIT scores on-the-fly...[/yellow]")

    # Import scorers inside to avoid circular imports
    from us_picker.scoring.factors.buffett import BuffettScorer
    from us_picker.scoring.factors.dcf import DCFScorer
    from us_picker.scoring.factors.graham import GrahamScorer
    from us_picker.scoring.factors.lynch import LynchScorer
    from us_picker.scoring.factors.magic_formula import MagicFormulaScorer
    from us_picker.scoring.factors.momentum import MomentumScorer
    from us_picker.scoring.factors.piotroski import PiotroskiScorer
    from us_picker.scoring.factors.technical import TechnicalScorer
    from us_picker.scoring.factors.dividend import DividendYieldScorer
    from us_picker.scoring.models.banking import BankingScorer
    from us_picker.scoring.models.holding import HoldingScorer
    from us_picker.scoring.models.reit import ReitScorer
    from us_picker.scoring.models.insurance import InsuranceScorer
    from us_picker.scoring.normalizer import ScoreNormalizer
    from us_picker.scoring.context import ScoringContext
    from us_picker.classification.risk_classifier import RiskClassifier
    from us_picker.scoring.red_flags import detect_flags

    companies = _scoring_universe(session, scoring_date)
    if not companies:
        return True

    buffett = BuffettScorer()
    dcf = DCFScorer()
    graham = GrahamScorer()
    piotroski = PiotroskiScorer()
    lynch = LynchScorer()

    context = ScoringContext(session, scoring_date)
    all_ids = [c.id for c in companies]
    context.load_data(all_ids)

    _FACTOR_COLS = [
        "buffett_score", "graham_score", "piotroski_fscore",
        "magic_formula_rank", "lynch_peg_score", "momentum_score",
        "technical_score",
    ]

    raw_scores = {}
    for company in companies:
        cid = company.id
        row = {
            "company_id": cid,
            "model_used": company.company_type or "OPERATING",
        }

        b = buffett.score(cid, session, scoring_date=scoring_date, scoring_context=context)
        row["buffett_score"] = (b or {}).get("buffett_combined")

        d = dcf.score(cid, session, scoring_date=scoring_date, scoring_context=context)
        row["dcf_margin_of_safety_pct"] = (d or {}).get("dcf_combined")
        if d is not None:
            row["dcf_intrinsic_value"] = d.get("intrinsic_value_per_share")
            growth = d.get("growth_rate_used")
            disc = d.get("discount_rate_used")
            term = d.get("terminal_growth_used")
            row["dcf_growth_rate_pct"] = round(growth * 100.0, 2) if growth is not None else None
            row["dcf_discount_rate_pct"] = round(disc * 100.0, 2) if disc is not None else None
            row["dcf_terminal_growth_pct"] = round(term * 100.0, 2) if term is not None else None

        g = graham.score(cid, session, scoring_date=scoring_date, scoring_context=context)
        row["graham_score"] = (g or {}).get("graham_combined")

        p = piotroski.score(cid, session, scoring_date=scoring_date, scoring_context=context)
        row["piotroski_fscore"] = (p or {}).get("fscore_total")
        row["piotroski_fscore_raw"] = int((p or {}).get("fscore_total", 0)) if p else None

        lynch_result = lynch.score(cid, session, scoring_date=scoring_date, scoring_context=context)
        row["lynch_peg_score"] = (lynch_result or {}).get("peg_score")

        raw_scores[cid] = row

    # Point-in-time universe passed to every cross-sectional scorer so that
    # names which were still listed on scoring_date but have since delisted
    # (resurrected by ``_scoring_universe`` via the survivorship as-of proxy)
    # get momentum/technical/magic/dividend/sector scores too. Without this the
    # score_all default ``is_active=True`` query dropped them, leaving those
    # rows with a value/quality-only composite (audit CRITICAL #4 follow-up,
    # 2026-07-06). ``all_ids`` already excludes INDEX/MOCK/TEST, so nothing
    # spurious is resurrected. Live scoring never passes this and is unchanged.

    # Magic Formula
    mf_scores = MagicFormulaScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in mf_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["magic_formula_rank"] = result.get("magic_formula_score")

    # Momentum
    mom_scores = MomentumScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in mom_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["momentum_score"] = result.get("momentum_combined")

    # Technical
    tech_scores = TechnicalScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in tech_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["technical_score"] = result.get("technical_score")
            raw_scores[cid]["above_200ma"] = result.get("above_200ma")

    # Dividend Yield
    div_scores = DividendYieldScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in div_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["dividend_score"] = result.get("dividend_score")

    # Banking
    bank_scores = BankingScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in bank_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["banking_composite"] = result.get("banking_composite")
            raw_scores[cid]["data_completeness"] = result.get("data_completeness")

    # Holding
    hold_scores = HoldingScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in hold_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["holding_composite"] = result.get("holding_composite")
            raw_scores[cid]["data_completeness"] = result.get("data_completeness")

    # REIT
    reit_scores = ReitScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in reit_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["reit_composite"] = result.get("reit_composite")
            raw_scores[cid]["data_completeness"] = result.get("data_completeness")
            raw_scores[cid]["model_used"] = "REIT"

    # Insurance
    ins_scores = InsuranceScorer().score_all(session, scoring_date=scoring_date, company_ids=all_ids)
    for cid, result in ins_scores.items():
        if cid in raw_scores:
            raw_scores[cid]["banking_composite"] = result.get("banking_composite")
            raw_scores[cid]["data_completeness"] = result.get("data_completeness")
            raw_scores[cid]["model_used"] = "INSURANCE"

    # Save initial
    for cid, row in raw_scores.items():
        session.add(ScoringResult(
            company_id=cid,
            scoring_date=scoring_date,
            pipeline_version=SCORING_PIPELINE_VERSION,
            model_used=row.get("model_used"),
            buffett_score=row.get("buffett_score"),
            graham_score=row.get("graham_score"),
            piotroski_fscore=row.get("piotroski_fscore"),
            piotroski_fscore_raw=row.get("piotroski_fscore_raw"),
            magic_formula_rank=row.get("magic_formula_rank"),
            lynch_peg_score=row.get("lynch_peg_score"),
            momentum_score=row.get("momentum_score"),
            technical_score=row.get("technical_score"),
            dcf_margin_of_safety_pct=row.get("dcf_margin_of_safety_pct"),
            dcf_intrinsic_value=row.get("dcf_intrinsic_value"),
            dcf_growth_rate_pct=row.get("dcf_growth_rate_pct"),
            dcf_discount_rate_pct=row.get("dcf_discount_rate_pct"),
            dcf_terminal_growth_pct=row.get("dcf_terminal_growth_pct"),
            dividend_score=row.get("dividend_score"),
            banking_composite=row.get("banking_composite"),
            holding_composite=row.get("holding_composite"),
            reit_composite=row.get("reit_composite"),
            data_completeness=row.get("data_completeness"),
            above_200ma=row.get("above_200ma")
        ))
    session.flush()

    # Normalization
    rows = (
        session.query(ScoringResult, Company)
        .join(Company, Company.id == ScoringResult.company_id)
        .filter(ScoringResult.scoring_date == scoring_date)
        .all()
    )
    if rows:
        df = pd.DataFrame([
            {
                "id": sr.id,
                "sector": c.sector_custom or c.sector_bist or "UNKNOWN",
                **{col: getattr(sr, col) for col in _FACTOR_COLS},
            }
            for sr, c in rows
        ]).set_index("id")

        normalizer = ScoreNormalizer()
        for col in _FACTOR_COLS:
            if col in df.columns and df[col].notna().any():
                df[col] = normalizer.normalize_factor(df, col, "sector")

        for row_id, row_data in df.iterrows():
            sr = session.get(ScoringResult, int(row_id))
            if sr:
                for col in _FACTOR_COLS:
                    val = row_data.get(col)
                    setattr(sr, col, None if pd.isna(val) else float(val))
        session.flush()

    # Risk tiers
    try:
        risk_clf = RiskClassifier()
        risk_clf.classify_all(session, scoring_date=scoring_date)
    except Exception as exc:
        if console:
            console.print(f"[yellow]Risk classification failed for {scoring_date}: {exc}[/yellow]")

    # Red flags
    try:
        from us_picker.scoring.red_flags import serialize_flags
        flag_rows = (
            session.query(ScoringResult)
            .filter(ScoringResult.scoring_date == scoring_date)
            .all()
        )
        for sr in flag_rows:
            flags = detect_flags({
                "piotroski_fscore_raw": sr.piotroski_fscore_raw,
                "data_completeness": sr.data_completeness,
                "dcf_margin_of_safety_pct": sr.dcf_margin_of_safety_pct,
                "technical_score": sr.technical_score,
            })
            sr.quality_flags_json = serialize_flags(flags)
        session.flush()
    except Exception as exc:
        if console:
            console.print(f"[yellow]Red flag detection failed for {scoring_date}: {exc}[/yellow]")

    # Compose. Unlike risk labels/flag decoration, this is the actual cache
    # payload and therefore fails hard through the guarded helper.
    _compose_scores_for_date(session, scoring_date, console=console)

    session.commit()
    return True


def _compose_scores_for_date(
    session: Session,
    scoring_date: date,
    console=None,
) -> None:
    """Compose one score date or leave it explicitly stale on failure."""
    from us_picker.scoring.composer import ScoreComposer

    try:
        ScoreComposer().compose_all(
            session,
            scoring_date=scoring_date,
            use_regime=False,
        )
    except Exception as exc:
        # A fail-soft path here previously committed factor rows as "current"
        # with every composite NULL, making the backtest silently sit in cash.
        logger.exception("Composition failed for %s", scoring_date)
        (
            session.query(ScoringResult)
            .filter(ScoringResult.scoring_date == scoring_date)
            .update(
                {ScoringResult.pipeline_version: None},
                synchronize_session=False,
            )
        )
        session.commit()
        if console:
            console.print(
                f"[yellow]Composition failed for {scoring_date}: {exc}[/yellow]"
            )
        raise RuntimeError(
            f"Composition failed for {scoring_date}; score cache left stale"
        ) from exc
