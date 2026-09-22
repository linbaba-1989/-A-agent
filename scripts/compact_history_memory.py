"""Offline P1.10.2 memory experiment. Never imports or constructs a Provider.

Run each implementation in a fresh process, e.g.:
  python scripts/compact_history_memory.py --kind legacy --output legacy.json
  python scripts/compact_history_memory.py --kind compact --output compact.json
RSS includes tracemalloc's own allocator overhead; report both independently.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime
import gc
import json
from pathlib import Path
import sys
from time import perf_counter
import tracemalloc

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.compact_market_history import CompactMarketHistory, RealtimePoint
from src.market_clock import to_beijing


class LegacyHistory:
    """Original retained object layout, including all five per-tick floats."""
    def __init__(self):
        self._points = defaultdict(lambda: deque(maxlen=300))
        self._sessions = {}

    def append(self, symbol, point):
        session = CompactMarketHistory.trading_session(point.timestamp)
        if session is None:
            return
        series = self._points[symbol]
        if self._sessions.get(symbol) != session:
            series.clear()
            self._sessions[symbol] = session
        if series and point.timestamp < series[-1].timestamp:
            return
        if series and point.timestamp == series[-1].timestamp:
            series[-1] = point
        else:
            series.append(point)


def gc_counts():
    gc.collect()
    objects = gc.get_objects()
    return {"gc_object_count": len(objects),
            "realtime_point_count": sum(isinstance(obj, RealtimePoint) for obj in objects),
            "deque_count": sum(isinstance(obj, deque) for obj in objects)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("legacy", "compact"), required=True)
    parser.add_argument("--symbols", type=int, default=5553)
    parser.add_argument("--snapshots", type=int, default=840)
    parser.add_argument("--no-tracing", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.snapshots < 300:
        parser.error("at least 300 snapshots are required")
    process = psutil.Process()
    symbols = [f"S{index:06d}" for index in range(args.symbols)]
    base = to_beijing(datetime(2026, 9, 16, 10)).timestamp()
    initial_counts = gc_counts()
    initial_rss = process.memory_info().rss
    if not args.no_tracing:
        tracemalloc.start(1)
    history = LegacyHistory() if args.kind == "legacy" else CompactMarketHistory()
    if args.kind == "compact":
        history.reserve_symbols(symbols)
    records = []
    started = perf_counter()
    checkpoints = {300, 420, args.snapshots}
    for frame in range(args.snapshots):
        for index, symbol in enumerate(symbols):
            stamp = base + float(frame)
            price = 10.0 + index * .001 + frame * .0001
            if args.kind == "legacy":
                history.append(symbol, RealtimePoint(stamp, price, float(frame + 100),
                                                    float(frame + 10000), float(frame / 1000)))
            else:
                history.append_price(symbol, stamp, price)
        if frame + 1 not in checkpoints:
            continue
        counts = gc_counts()
        traced, peak = tracemalloc.get_traced_memory()
        record = {"snapshot_count": frame + 1, "elapsed_seconds": perf_counter() - started,
                  "rss_bytes": process.memory_info().rss,
                  "rss_delta_bytes": process.memory_info().rss - initial_rss,
                  "python_traced_bytes": traced, "python_peak_bytes": peak,
                  "tracer_overhead_bytes": tracemalloc.get_tracemalloc_memory(),
                  "gc_objects_added": counts["gc_object_count"] - initial_counts["gc_object_count"],
                  **counts}
        if args.kind == "compact":
            record.update(history.diagnostics())
            assert counts["realtime_point_count"] == initial_counts["realtime_point_count"]
            assert history._timestamps.dtype.name == history._prices.dtype.name == "float64"
        else:
            total = sum(map(len, history._points.values()))
            sample = history._points[symbols[0]][-1]
            per_point = sys.getsizeof(sample) + sys.getsizeof(sample.__dict__) + 5 * sys.getsizeof(1.0)
            record.update(snapshot_points=total, snapshot_symbols=len(history._points),
                          estimated_bytes=total * per_point + sys.getsizeof(history._points) +
                          sum(sys.getsizeof(value) for value in history._points.values()),
                          point_including_five_floats_bytes=per_point)
            assert counts["realtime_point_count"] - initial_counts["realtime_point_count"] == total
            del sample
        records.append(record)
        print(json.dumps(record), flush=True)
    if args.kind == "compact" and args.snapshots > 420:
        assert records[-1]["snapshot_points"] == records[-2]["snapshot_points"] == args.symbols * 361
        assert records[-1]["history_storage_bytes"] == records[-2]["history_storage_bytes"]
        assert history.speed(symbols[0], 5) != "unavailable"
    result = {"kind": args.kind, "symbols": args.symbols, "initial_rss_bytes": initial_rss,
              "initial_gc": initial_counts, "tracing": not args.no_tracing, "samples": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
