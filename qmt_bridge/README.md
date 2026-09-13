# QMT Internal Python Market Bridge POC

`qmt_internal_market_bridge.py` runs inside the Dongguan Securities QMT built-in Python 3.6 strategy environment. It subscribes to three real tick streams and publishes a read-only snapshot to `C:\A-agent-bridge\latest_ticks.json` every second.

1. Open the QMT Python strategy editor and create a strategy using the contents of `qmt_internal_market_bridge.py`.
2. Run it as a live quote-driven strategy. No trading account API or order function is used.
3. Confirm the strategy log contains `A_AGENT_BRIDGE_STARTED subscription_id=<positive integer>`.
4. From the A-agent project, run `python scripts/qmt_bridge_probe.py --watch-seconds 5`.

The callback only replaces entries in an in-memory dictionary. The timer writes a temporary file, flushes it to disk, and calls `os.replace`, so readers see either the previous complete JSON document or the next complete document.

The first POC intentionally subscribes only to `600498.SH`, `600000.SH`, and `000001.SZ`. Expanding the code list requires a separate throughput and QMT subscription-entitlement test.
