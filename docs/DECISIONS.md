# DECISIONS

Standing decisions with rationale. Changed decisions are marked
`SUPERSEDED by Dxxx`, never deleted.

| ID | Decision | Rationale |
|---|---|---|
| D001 | Free-data policy. The ONLY sanctioned paid item is a one-month EODHD All-World subscription (~$20) to snapshot delisted-inclusive US price history for the honest backtest. | Fundamentals are free and survivorship-clean from EDGAR; prices are the one gap money must close. A survivorship-biased small-cap backtest is worse than none. |
| D002 | PIT rule: a fact is usable at the first session open ≥ its EDGAR `acceptanceDateTime`; the PIT store is as-first-filed (MIN(accepted), insert-only); `prevrpt` and the `frames` API are banned from PIT paths. | Look-ahead and restatement leakage are the two classic backtest poisons; both are structural here, not policy. |
| D003 | v1 universe excludes: OTC, FPIs/ADRs (20-F filers), shells/blank-checks (SIC 6770 now-or-ever, cover-page shell flag), pre-revenue biotech, financials and REITs (from generic scoring). | Each has a mechanical reason (no quarterly data, undefined factors, fraud density) — not a style preference. Carve-out models may come in v2. |
| D004 | "Previous close" = official closing-auction price (NOCP / NYSE auction), never a vendor overnight last-trade. | After-hours prints on thin names corrupt signals; the official close is the only reproducible T-1 reference. |
| D005 | Execution contract: T-1 data → Istanbul-morning decision → LOO orders with ±3-5% collar staged before 09:28 ET → T opening auction fill. Backtest, paper-forward and live use the identical contract, including unfilled-order handling. | Backtest=live parity was the hardest-won MobileInv lesson; MOO in small caps is uncollared gap risk; 09:28 (not 09:30) is the real Nasdaq cutoff. |
| D006 | Raw prices are immutable; adjustments live separately and are recomputable; vendor adjusted closes are never the primary store. | Vendors rewrite adjusted history on every action; reproducibility dies otherwise. (Ported from MobileInv.) |
| D007 | Performance is never reported as a lone number: always net-of-cost, vs Russell 2000 TR (primary) and S&P 500 TR, with MaxDD/Ulcer/rolling-12m consistency. USD is the base; TRY/gold views are display-only. | Prevents the nominal-headline self-deception the BIST project caught with gold-deflation (its D009). |
| D008 | Parameters come from the pre-registered grid via walk-forward plateau selection; single-best-cell picks and post-hoc grid edits are protocol violations; TEST is touched once. | 500+ cells guarantee a lucky winner; plateaus + one-shot TEST are the defense. |
| D009 | This repo produces signals and paper-forward evidence only. Capital decisions are outside its scope and require the paper-forward window to complete first. | Mirrors the BIST forward-window rule; keeps the system honest and the operator patient. |
| D010 | yfinance is banned from all scheduled/automated paths (manual prototyping only). | Operationally broken for unattended use (429 waves, dependency breakage, ToS gray). |
| D011 | A feature that isn't fed (no data/coverage) or doesn't gate is not a feature — it is removed or wired as a hard gate with coverage checks. | Ported from MobileInv (dead CDS/insider/event inputs); red flags here BLOCK selection, not decorate it. |
| D012 | Zero runtime coupling to MobileInv: no shared code imports, separate repos/state/delivery; lessons are ported as specs, code only by copy-with-tests. | Two markets, two data contracts; coupling would let a US change silently break the live BIST system (or vice versa). |
| D013 | All schedules pin to America/New_York; all stored timestamps tz-aware; XNYS exchange calendar is the only source of trading days. | Istanbul has no DST, ET does — the offset shifts twice a year; hand-rolled calendars always get Good Friday or observed-July-4th wrong. |
| D014 | Wrong changes are reverted via revert PR; destructive git (force-push, reset --hard on shared trees) is forbidden. | Ported from MobileInv after a real data-loss incident there. |
