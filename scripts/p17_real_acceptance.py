"""One-shot P1.7 real market acceptance. Read-only; never calls model or trading APIs."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median
import sys
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.market_data_router import MarketDataRouter
from src.realtime_market import RealtimeMarketFeed, live_state, rank_rows


TARGETS = ["600498.SH", "600000.SH", "000001.SZ", "000636.SZ", "000823.SZ"]
DURATION_SECONDS = 620
POLL_SECONDS = 2
PAUSE_AT_SECONDS = 360
PAUSE_SECONDS = 10
OUTPUT = Path("outputs/acceptance/p17_real_market.json")


def main() -> int:
    load_dotenv(".env")
    started_at = datetime.now()
    if started_at.weekday() >= 5 or not (13 <= started_at.hour < 15):
        raise RuntimeError("continuous_trading_session_required")
    selection = MarketDataRouter().select()
    provider = selection.provider
    if provider is None or getattr(provider, "provider_name", "") != "XtDataCenter Token":
        raise RuntimeError("xtdatacenter_primary_required")
    feed = RealtimeMarketFeed(provider)
    rounds, latencies, ui_latencies = [], [], []
    observed_flashes = {symbol: {"up": 0, "down": 0, "unchanged": 0} for symbol in TARGETS}
    top_signatures = []
    pause_result = None
    previous_timestamp = None
    deadline = time.monotonic() + DURATION_SECONDS
    next_run = time.monotonic()
    paused = False
    try:
        while time.monotonic() < deadline:
            elapsed = DURATION_SECONDS - (deadline - time.monotonic())
            if not paused and elapsed >= PAUSE_AT_SECONDS:
                calls_before = feed.provider_request_count
                time.sleep(PAUSE_SECONDS)
                pause_result = {"seconds": PAUSE_SECONDS, "requests_before": calls_before,
                                "requests_after": feed.provider_request_count,
                                "stopped": calls_before == feed.provider_request_count}
                paused = True
                next_run = time.monotonic()
            rows = feed.snapshot()
            latency = feed.snapshot_latency
            if rows:
                timestamp = max(row["quote_timestamp"] for row in rows)
                if datetime.fromtimestamp(timestamp).date() != started_at.date():
                    raise RuntimeError("tick_date_not_today")
                view_started = time.perf_counter()
                top = feed.enrich_static(rank_rows(rows, "change_pct", 20))
                ui_latency = time.perf_counter() - view_started
                top_signatures.append(tuple(row["symbol"] for row in top))
                by_symbol = {row["symbol"]: row for row in rows}
                targets = {}
                for symbol in TARGETS:
                    row = by_symbol.get(symbol)
                    if not row:
                        continue
                    flash = row["flash_class"]
                    observed_flashes[symbol]["up" if flash.endswith("up") else "down" if flash.endswith("down") else "unchanged"] += 1
                    targets[symbol] = {key: row.get(key) for key in (
                        "quote_time", "lastPrice", "amount", "volume", "speed_1m", "speed_3m", "speed_5m", "flash_class")}
                rounds.append({"wall_time": datetime.now().isoformat(timespec="seconds"),
                               "snapshot_time": datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
                               "snapshot_latency": latency, "valid_quotes": len(rows),
                               "live_state": live_state(previous_timestamp, timestamp), "targets": targets})
                previous_timestamp = timestamp
                latencies.append(latency); ui_latencies.append(ui_latency)
            next_run += POLL_SECONDS
            time.sleep(max(0, next_run - time.monotonic()))

        # Simulate a simultaneous late fragment: it must use cache and make no real request.
        feed._lock.acquire()
        try:
            calls_before_lock = feed.provider_request_count
            locked_rows = feed.snapshot(TARGETS)
        finally:
            feed._lock.release()
        lock_audit = {"cached_rows": len(locked_rows), "requests_before": calls_before_lock,
                      "requests_after": feed.provider_request_count}
        payload = {
            "started_at": started_at.isoformat(timespec="seconds"),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "provider": provider.provider_name,
            "provider_init_count": feed.provider_initializations,
            "provider_request_count": feed.provider_request_count,
            "skipped_due_to_lock": feed.skipped_due_to_lock,
            "overlapping_request_count": feed.overlapping_request_count,
            "lock_audit": lock_audit,
            "snapshot_count": len(rounds),
            "snapshot_latency_average": mean(latencies) if latencies else None,
            "snapshot_latency_p50": median(latencies) if latencies else None,
            "snapshot_latency_max": max(latencies) if latencies else None,
            "ui_latency_average": mean(ui_latencies) if ui_latencies else None,
            "ui_latency_max": max(ui_latencies) if ui_latencies else None,
            "quote_time_progressed": bool(rounds and rounds[-1]["snapshot_time"] > rounds[0]["snapshot_time"]),
            "observed_flashes": observed_flashes,
            "pause_resume": pause_result,
            "top20_distinct_snapshots": len(set(top_signatures)),
            "rounds": rounds,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in payload.items() if key != "rounds"}, ensure_ascii=False))
        return 0
    finally:
        if hasattr(provider, "close"):
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
