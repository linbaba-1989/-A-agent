# V1 Holiday Sprint

## Day 1 / Foundation completed

Accepted on 2026-09-30 (Asia/Shanghai).

Code baseline SHA: `68d630663d57d8d2441d97222cb7732c781ea879`.
This is the validated code baseline; the following documentation-only commit
records it without changing runtime behavior.

- P2.2 Realtime Fast Lane: `1acf1d59c9c298a336e5fe8a58952dcefbf4fffd`.
  Current-symbol and watchlist quotes target approximately 1 second during
  trading sessions. This is a polling target, not a latency guarantee or
  exchange tick feed; closed-session polling backs off to at least 15 seconds.
- Full Market remains independent, including its provider state, cache, SSE
  channel and Top20. Fast Lane retains its 50-symbol subscription limit.
- Scanner scans the full universe before applying all selected filters with
  strict AND. Missing required values and NaN/Inf are rejected. Invalid ranges
  return before scanning. The UI shows the first 100 results; UTF-8-SIG CSV
  exports all filtered rows. Integer scan limits retain their prior behavior.
- Sector preserves stale-cache reporting and supported scope. Local acceptance
  observed 709/710, `COMPLETE_SUPPORTED_SCOPE`, `unsupported=1`.
- Production hybrid is **not formally approved**. Production routing and
  configuration remain unchanged; no production switch or trading integration
  is included in this baseline.

Validation: 749 tests passed, 0 failed; P2.2/Hybrid/Sector/SSE targeted suite
82 passed; `python -m compileall .` and `git diff --check` passed.
The prior local 8507 hybrid acceptance observed 5578 universe, 5574 valid rows,
233 results for 3%-5% change, 100 displayed and 233 exported; 210 qualifying
stocks ranked below the former top-100 cutoff. Acceptance was after hours with
cached quotes, not a live-session latency certification.

PR #1 (`4cab159d46d40fbc51248d33632469b01942a536`) was manually integrated with
range pre-validation corrected, then closed without merging after the code
baseline reached main. Its source branch is retained. Local evidence, caches,
databases, screenshots, logs and temporary acceptance harnesses are excluded
from these baseline commits.

## Next stage

Day 2 = Historical Coverage & Data Foundation.

Day 2 has not started. MinuteBarBuilder, KDJ, RSI, 30m MACD, late-session
strategies, new AI Agent features, production switching and trading interfaces
remain outside this baseline task.
