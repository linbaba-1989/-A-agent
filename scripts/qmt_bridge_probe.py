"""Read and validate the atomic snapshot produced by QMT internal Python."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

EXPECTED_CODES = {"600498.SH", "600000.SH", "000001.SZ"}
REQUIRED_FIELDS = {
    "code", "time", "lastPrice", "lastClose", "open", "high", "low",
    "volume", "pvolume", "amount", "stockStatus",
}
DEFAULT_PATH = Path(r"C:\A-agent-bridge\latest_ticks.json")


class BridgeSnapshotError(RuntimeError):
    pass


def read_snapshot(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeSnapshotError(f"bridge_snapshot_unreadable: {type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("source") != "QMT_INTERNAL_PYTHON":
        raise BridgeSnapshotError("bridge_snapshot_header_invalid")
    ticks = payload.get("ticks")
    if not isinstance(ticks, dict) or set(ticks) != EXPECTED_CODES:
        raise BridgeSnapshotError(f"bridge_codes_invalid: expected={sorted(EXPECTED_CODES)}, actual={sorted(ticks) if isinstance(ticks, dict) else ticks}")
    for code in sorted(EXPECTED_CODES):
        tick = ticks[code]
        if not isinstance(tick, dict) or not REQUIRED_FIELDS.issubset(tick):
            raise BridgeSnapshotError(f"bridge_tick_structure_invalid: {code}")
        if tick.get("code") != code:
            raise BridgeSnapshotError(f"bridge_tick_code_invalid: {code}")
        try:
            if float(tick["time"]) <= 0:
                raise ValueError("non-positive time")
            if float(tick["lastPrice"]) <= 0 or float(tick["lastClose"]) <= 0:
                raise ValueError("non-positive price")
        except (TypeError, ValueError) as exc:
            raise BridgeSnapshotError(f"bridge_tick_value_invalid: {code}: {exc}") from exc
    return payload


def tick_times(payload: dict[str, Any]) -> dict[str, float]:
    return {code: float(payload["ticks"][code]["time"]) for code in EXPECTED_CODES}


def times_advanced(before: dict[str, Any], after: dict[str, Any]) -> bool:
    first, second = tick_times(before), tick_times(after)
    return any(second[code] > first[code] for code in EXPECTED_CODES)


def probe(path: str | Path = DEFAULT_PATH, watch_seconds: float = 0.0) -> dict[str, Any]:
    before = read_snapshot(path)
    result = {
        "success": True,
        "path": str(Path(path)),
        "codes": sorted(EXPECTED_CODES),
        "written_at_ms": before.get("written_at_ms"),
        "tick_times": tick_times(before),
        "time_advanced": None,
    }
    if watch_seconds > 0:
        time.sleep(watch_seconds)
        after = read_snapshot(path)
        result["time_advanced"] = times_advanced(before, after)
        result["latest_tick_times"] = tick_times(after)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate the QMT internal Python bridge snapshot")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--watch-seconds", type=float, default=0.0)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.path, args.watch_seconds), ensure_ascii=False, indent=2))
    except BridgeSnapshotError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
