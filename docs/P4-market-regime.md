# P4 Market Regime Engine and Conditional Strategy Evaluation

P3 local baseline: `8b1f7c89f25a39c1b767bf9e7152d99acd42d80f`.
P2.3 remote baseline: `aa232dacd0a8dbb829a0f8abbb9d82b4032d81f5`.
P4 uses the existing selected raw local history. It does not download bars,
call a paid LLM, change production providers, retrain P3 patterns or place orders.

## Structure and time contract

`src/regime/features.py` reconstructs cross-sectional daily features on the
stored exchange calendar and the current 5,578-stock universe. SQL runs
through temporary views/tables. A missing stock/session stays NULL: a return
over a suspension is not silently relabeled as a one-session return. Moving
averages require all 5/20/60 calendar sessions. Ratios have explicit valid
denominators; return and indicator coverage are retained. Unverified listing
dates remain unknown rather than inferred. Current-universe selection has
survivorship bias; the dataset is not a point-in-time historical constituent set.

The three index series are 000001.SH, 399001.SZ and 399006.SZ. Their features
include 1/3/5/20-session returns, MA5/10/20/60, MA20 slope, RSI, MACD/histogram,
ATR, realized volatility, 20/60-session drawdown and 30m trend/MACD/volatility.
The final three afternoon bars reconstruct market-wide 30m acceleration and
participation. The day-level classifier consumes these only after that close;
this V1 does not claim to provide an intraday/live classification.

For session D, `data_end_time=D 15:00`, modeled availability `as_of=D 15:01`.
`created_at` records the actual backfill computation time. Historical records
model what could have been known then; they are not claimed to have actually
been published in the past. A query at 09:30 or 15:00 cannot see that day's
closing state. Date-only API inputs mean end of that date. Timestamp inputs
are exact, with aware timestamps converted to Asia/Shanghai.

Raw feature allowlists reject outcome/label additions at calculation and
persistence boundaries. Later returns live only in evaluation statistics.
No backward fill, full-history normalization or shuffled fit is used.

## Scores and rules

`engine.py` retains every raw feature and a weighted component breakdown.
Each component uses fixed clipping intervals, specified before the reported
benchmark outcomes. No profitability-driven threshold tuning is performed.

- Trend score: index MA position 20%, slope 10%, breadth 10%, MA20/60-above
  participation 20%, new-high/new-low balance 10%, persistence 15%, drawdown
  10%, volume confirmation 5%.
- Short score: net large positive breadth 25%, high-volatility participation 10%, amount
  share of large moves 10%, top-decile amount concentration 10%, one-session
  momentum persistence 15%, three-session persistence 15%, 30m acceleration 15%.
- Risk score: drawdown 20%, volatility 15%, decline breadth 20%, new-low
  expansion 10%, dispersion 10%, index trend breaks 15%, liquidity contraction 10%.

Scores range 0–100. Risk levels: below 40 LOW, 40–65 MEDIUM, 65+ HIGH.
High-volatility participation is the fraction of valid stocks with 20-session
daily volatility >=2.5%, a positive session and above-average amount. Large
single-session moves are separately retained; they are not mislabeled as
historically high volatility.
Missing index/warmup inputs, return or 30m coverage below 80%, or 60-session
indicator coverage below 70%, produces UNKNOWN rather than an imputed score.
The early warmup dates are retained, not removed from the history.

Classification priority is RISK_OFF (risk >=65), TREND_STRONG (trend >=68,
risk <50), SHORT_HOT (short >=68, risk <55), SHORT_REPAIR (short >=50,
risk <60, risk has fallen >=8 from one of the preceding three valid closes,
positive index three-session return and advance ratio >50%), TREND_ROTATION
(trend >=52, risk <55), otherwise MIXED. Directional states need two consecutive
candidate closes; risk reduction and MIXED are immediate. UNKNOWN resets
confirmation and repair state. Confidence is coverage plus rule margin,
**not a calibrated probability of profitable trading**.

Six-center deterministic clustering is fitted only to valid training scores
and frozen thereafter. It is an auxiliary diagnostic, never a feature,
classifier override, strategy selector or source of historical relabeling.

Historical ST/IPO/special-session limit metadata is incomplete. Limit-up/down
counts are UNKNOWN. No 9.5% shortcut, consecutive-limit height, failed-limit
rate, leader or emotion-cycle field is fabricated.

## Prespecified benchmark families

The same fixed ten P3 stocks are used, with CNY 100,000 per stock and a 25%
per-account position limit. Results describe this cohort, not a market-wide
stock-selection backtest. Inactive accounts remain cash. Portfolio NAV sums
the ten independent accounts; return percentages are not summed.

Trend: prior 20-session average amount >= CNY 100m, positive volume,
close > MA20 > MA60, rising MA20, positive 20-session strength relative to
the Shanghai index, and either a prior-20-session high breakout or a positive
session within 2.5% above MA20. Fixed stop/take/holding: 8% / 20% / 20 sessions.

Short: same liquidity gate, positive current return, three-session momentum
above 2%, positive relative three-session strength, amount >1.1 times the
prior 20-session average and daily volatility between 0.8% and 6%.
Fixed stop/take/holding: 4% / 8% / 3 sessions.

Signals are edge-triggered at 15:01 and first attempt the next open. P3's
unchanged T+1, fees/tax/slippage, cash/quantity and no-fill constraints apply.
Ordinary-board price-limit estimates remain an explicit limitation without
historical ST/IPO/ex-right overrides. Raw-price research does not invent
corporate-action total returns. These are diagnostic families, not final strategies.

The chronological 60/20/20 split has no shuffle. Indicators may use prior
history; execution accounts start fresh per split. Terminal positions remain
marked to market rather than receiving fictitious liquidation prices. Each
regime-specific simulation gates entry by the state available at signal time;
it does not attribute exits to the regime observed later at exit time.

Metrics retain trades, win rate, expectancy, win/loss ratio, profit factor,
drawdown, Sharpe, total return, open positions and blocked orders. Confidence
uses unique entry-signal dates, with returns averaged across same-date stocks.
This reduces cross-stock duplication but does not prove serial independence.
Conservative normal-approximation bounds adjust across 14 family/state comparisons;
they are exploratory diagnostics, not a formal guarantee for heavy-tailed returns.

Preference requires >=10 independent entry dates for both families and >=10
paired nonoverlapping five-session return blocks. A directional preference
also needs positive lower-bound expectancy, net profit factor >1, no worse
drawdown, and a paired return-difference interval favoring that family. OOS
cannot newly select a direction that was not consistent in train and validation.
Both expectancy upper bounds below zero produce RISK_OFF; inadequate evidence
produces INSUFFICIENT_EVIDENCE. The current classifier's risk-control RISK_OFF
can independently override an empirical family preference. Win rate alone
never selects a family. Statistics are unavailable to historical APIs until
after their own evidence window ends.

## P3 conditioning

`conditioning.py` reads the frozen 20 P3 pilot profiles and their original
pattern ids, splits and labels. An as-of join assigns the latest regime already
available at each original confirmation time. Consequently a daily P3 confirmation
at 15:00 receives the preceding available regime, not today's 15:01 state;
30m occurrences likewise cannot use that day's future closing context.

Outcomes crossing each split boundary are purged. Raw and nonoverlapping effective
sample counts remain distinct: <5 INSUFFICIENT, 5–9 LOW_CONFIDENCE, >=10 NORMAL.
Conditional support requires at least ten effective observations in each of
train/validation/OOS, positive adjusted bounds above costs and the unconditional
pattern mean, and positive-rate lower bounds above 0.5. Adjustments cover
patterns × regimes × five horizons within each stock/period. No market-wide
family-wise significance claim is made. Five sessions is the prespecified
support horizon. Conditional support is research evidence, not an automatically
created strategy; P3 identities, candidates and backtests are not rewritten.

## Storage and interfaces

Required tables: `market_regime_features`, `market_regimes`,
`regime_strategy_statistics`. Separate `pattern_regime_statistics` stores
conditional outcomes. Keys include as_of, frequency, model_version and config_key;
statistics additionally identify family/split or stock/pattern/regime/horizon.
All records carry model_version, data_end_time and created_at. One transaction
commits a derived snapshot; backdated data_end_time is rejected. Reruns replace
only the same P4 version/config snapshot, never P2/P3 inputs.

Public read-only APIs in `src.regime`:

```python
get_current_market_regime()
get_market_regime('2026-09-30')
get_market_regime('2026-09-30T09:30:00')
get_regime_history('2025-01-01', '2026-09-30')
```

Run `scripts/regime_research.py --output <local-report.json> --progress <local-progress.json>`
after core validation. It needs >=15 GiB free and makes no network requests.
It audits historical row counts, the hash of P3 profiles and temporal availability.
Outputs, progress, logs and the DuckDB remain outside the source whitelist.

The simple Market Regime page reads stored evidence only. Behavior Lab displays
market context separately; a strong market never turns an unmatched stock
into a matched pattern. No P5/P6 engine, automatic paper/live trading or provider
switch is started by this phase.
