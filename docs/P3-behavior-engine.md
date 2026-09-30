# P3 Stock Behavior Pattern Engine

P2.3 baseline: `aa232dacd0a8dbb829a0f8abbb9d82b4032d81f5`.
P3 is local research and simulation. No broker, order submission, paid LLM,
production quote-source switch, or new history download is invoked.

## Time contracts

The input pool is 2024-10-01 through 2026-09-30. It is not a single two-year
trend signal. `SwingDetector` processes bars in order and uses a reversal
distance derived from causal Wilder ATR and rolling return volatility.
The scale is frozen at the candidate extremum; it is not a fixed price
percentage. The default multipliers are 2.5 ATR / 2 volatility, minimum
pivot separation 3 bars, and at least one subsequent bar to confirm.

Every pivot stores pivot_time, confirmation_time, price and direction.
Daily timestamps refer to the trading date; observation availability is
15:00. A 30m timestamp is the interval end. Pivots without confirmation
are not emitted, and appending bars does not revise emitted pivots.
Features attach to confirmation_time, never to an earlier extreme.

Segments contain return, close-path drawdown/runup, duration, ATR,
volatility, MA5/10/20/60, Wilder-style RSI14, KDJ9, MACD12/26/9 and
histogram, volume ratio/trends, amount trend, MA distances, 20/60 trading-day
high distances, and the previous confirmed swing. Indicator windows are
bar windows except day-high distances (8 regular 30m slots per session).
Warmup segments with unavailable inputs are excluded from model fitting.
Confirmed swing count and feature-eligible segment count are separate.

The current match uses the partial segment from the last confirmed pivot
to the latest observed close. Its endpoint is explicitly not a confirmed
pivot. Matching excludes the current record and future confirmations.
A frozen-model outlier remains unmatched; no fitted model means insufficient
evidence. All current results are as of their recorded data_end_date.

## Features, outcomes, and validation

`FEATURES` is an explicit allowlist. Outcome columns never enter the scaler
or cluster distance. Outcomes are separately labeled 1/3/5/10/20 subsequent
**trading days**, even for 30m. They start at the confirmation close and end
at the target session close, with MFE >= 0 and MAE <= 0. They are research
labels, not assumed achievable trade returns.

Chronological session split: 60% train, 20% validation, 20% untouched OOS.
No shuffle. Training outcomes crossing train_end are purged; validation
outcomes crossing validation_end are purged. Unmatured terminal outcomes
are censored. The scaler and deterministic farthest-first k-means are fitted
only on training features and frozen for validation/OOS. K is at most 5,
with a nominal 10 training occurrences per cluster. Rolling chronological
fold boundaries are exposed by `rolling_walk_forward`.

Default evidence labels: fewer than 5 INSUFFICIENT; 5–9 LOW_CONFIDENCE;
10+ NORMAL. Statistical summaries retain both raw occurrences and effective
non-overlapping outcomes. The latter drive evidence thresholds.
Mean lower bounds and Wilson positive-rate lower bounds use a Bonferroni
adjustment across a stock/period's cluster count. This is exploratory
per-stock evidence, not a market-wide family-wise significance claim.

Candidate selection is fixed using train and validation before OOS.
Default focus horizon is 5 trading days; all five horizons are reported.
A candidate requires train effective N >= 10, validation >= 5 and OOS >= 5,
positive-rate lower bound > 0.5, a mean lower bound above the configured
cost buffer plus the split's nonnegative unconditional mean, and at least
5 profitable-in-aggregate net OOS trades for the selected pattern.
MAE/MFE policy parameters use training outcomes only. Candidates created
after OOS are usable strictly after data_end_date, never retroactively in
the OOS used to approve them. Failure to pass produces
`NO_STATISTICALLY_USEFUL_PATTERN`, not a fabricated strategy.

## Execution and measurement

`BacktestEngine` is long-only, one position per symbol, configurable cash
and position fraction. Signals observed at a close attempt the next bar
open, subject to tradeability. A shared 30m boundary is processed after
the prior close; the recorded fill time is strictly later than signal_time.
Signal, order and fill times, price, fee and exit reason are retained.
T+1 prevents selling shares bought that session, including same-day 30m exits.
Close-observed stops/takes create next-open orders; there is no claim of
intrabar stop-price execution. Open terminal positions stay open and are
marked to market rather than liquidated at an unavailable future price.

Minimum buy quantity and quantity increments are separate parameters.
Defaults are 100/100 for main board and ChiNext; the research adapter uses
200/1 for STAR and 100/1 for BJ. No-volume or explicitly suspended bars
do not fill. Both price-limit boundaries conservatively reject fills.
Per-bar limit_up/limit_down metadata overrides estimates. The standalone
engine defaults to no fill when those limits are missing. The research
pilot explicitly enables ordinary-board limits estimated from prior
session close (10%, ChiNext/STAR 20%, BJ 30%). These are not a complete
historical IPO/ST/ex-rights reference dataset; unusual sessions require
explicit overrides. No historical status is fabricated from today's name.

Commission, minimum commission, transfer fee, sell stamp tax and slippage
are configurable. Illustrative research defaults: 0.03% commission,
CNY 5 minimum, 0.001% transfer fee, 0.05% sell stamp tax, 0.05% slippage.
Broker-specific costs and day-specific regimes can differ.
Raw data means price-return research: dividends, splits and total-return
accounting are not silently invented. These limitations remain in results.

Trade metrics include closed trades, win rate, mean win/loss, P/L ratio,
expectancy, profit factor, total/annualized return, drawdown, daily Sharpe,
loss streak and mean holding sessions. Annualization uses daily equity
returns and 252 sessions, including initial capital in drawdown.
With zero trades, trade win-rate/expectancy are unavailable, not a success.
Each stock is an independent account; their percentages are not summed
and presented as a portfolio return.

Pilot execution probes issue one fixed-time test buy in OOS without using
outcomes to choose it. They validate real-bar fills and costs; they are
explicitly **not pattern strategies** and never create strategy candidates.
At CNY 100,000 capital and 25% maximum allocation, expensive stocks may
correctly fail the minimum-lot affordability check.

## Frozen cohort and scope

The fixed cohort is selected before pattern outcomes:
600498.SH, 600519.SH, 600036.SH, 600900.SH, 002053.SZ,
000001.SZ, 000651.SZ, 002475.SZ, 300750.SZ, 300059.SZ.
It covers SH/SZ/ChiNext, banks/utilities/consumer/industrial/technology,
large-cap and a mid-cap proxy, with observed volatility and trend/range
descriptors reported separately rather than selected for profits.
002053's 2025 annual summary reports 920,729,464 shares; multiplied by the
local end-date raw close 9.52 gives about CNY 8.77bn, a sizing proxy rather
than a point-in-time historical market-cap feature.
BJ enters only with foundation ready=true; the P2.3 BJ minute sample did
not qualify. There are exactly 10 pilot stocks, including 600498.

Pilot computes both daily and 30m. All-A expansion is **daily only** and
requires a passing same-version/config 20-profile pilot. The fixed 5578
current-universe membership has survivorship bias. Selected-series unknown
gaps, structural errors and unfinished foundation windows are excluded;
less than 100 observed sessions is separately skipped. A skipped symbol is
not labeled as successfully analyzed. Whole-market 30m is not automatic.
The prior 60/120-session/on-demand 1m policy is unchanged.

## Persistence and operations

All derived tables share the existing ignored DuckDB:
behavior_segments, behavior_patterns, behavior_pattern_occurrences,
behavior_pattern_outcomes, behavior_pattern_statistics,
behavior_strategy_candidates, behavior_backtest_results,
behavior_profiles, behavior_jobs, behavior_job_config.

Feature tables have no outcome columns. Stored model artifacts contain
only feature allowlists/scales/centers; label/statistics tables are separate.
A per-symbol transaction commits the derived rows and DONE checkpoint
together. Input fingerprints, version, configuration and data end identify
an analysis. Re-running the same job skips DONE and SKIPPED; failures are
isolated and require an explicit subsequent repair job, not endless retries.
Changing a job's universe/configuration is rejected. These tables never
modify the historical bars or the foundation checkpoint tasks.

Use `scripts/behavior_profiles.py pilot` first, then `all-daily`.
Both require explicit job, output and atomic progress paths.
`--pilot-evidence` gates all-daily. `--stop-file` and Ctrl+C cooperate at
the next per-symbol checkpoint. `--max-new-symbols` exercises safe stopping.
Resume with the same arguments and no max-new-symbols cap.
Below 15 GiB free, no new symbol starts. Logs/progress/output/DB remain local,
outside the source whitelist. The database is single-writer; the read-only
Behavior Lab gracefully reports an update in progress when it is busy.

The Behavior Lab shows current matching, historical matches/differences,
sample counts, separate train/validation/OOS horizon statistics, candidates,
and recorded backtests. It does not compute on demand, access a paid LLM,
or submit orders. Core tests/pilot precede UI integration.

## Primary references

Simulation parameters are explicit assumptions, with rule references:
- [SSE trading rules, 2026 revision](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)
- [SSE STAR quantity increments](https://edu.sse.com.cn/tib/qa/c/4761956.shtml)
- [SZSE ChiNext price limits](https://www.szse.cn/www/investor/knowledge/t20200729_580056.html)
- [MOF stamp tax reduction notice](https://m.mof.gov.cn/czxw/202308/t20230827_3904226.htm)
- [002053 issuer 2025 annual summary](https://static.cninfo.com.cn/finalpage/2026-03-27/1225033290.PDF)
