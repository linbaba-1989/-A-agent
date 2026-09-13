#coding:gbk
"""Read-only QMT internal market bridge for Python 3.6."""
import json
import os
import threading
import time


CODES = ["600498.SH", "600000.SH", "000001.SZ"]
OUTPUT_DIR = r"C:\A-agent-bridge"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "latest_ticks.json")
TEMP_FILE = os.path.join(OUTPUT_DIR, "latest_ticks.json.tmp")
FIELDS = (
    "time", "lastPrice", "lastClose", "open", "high", "low",
    "volume", "pvolume", "amount", "stockStatus"
)

LATEST_TICKS = {}
LATEST_LOCK = threading.RLock()
SUBSCRIPTION_ID = None


def _plain_value(value):
    """Convert numpy-like scalar values without importing third-party modules."""
    try:
        return value.item()
    except AttributeError:
        return value


def _normalize_tick(code, tick):
    result = {"code": code}
    for field in FIELDS:
        result[field] = _plain_value(tick.get(field))
    return result


def on_whole_quote(data):
    """Keep callback minimal: copy the latest three ticks into memory."""
    if not isinstance(data, dict):
        return
    with LATEST_LOCK:
        for code, tick in data.items():
            if code in CODES and isinstance(tick, dict):
                LATEST_TICKS[code] = _normalize_tick(code, tick)


def flush(ContextInfo):
    """Write a complete snapshot and atomically replace the published file."""
    del ContextInfo
    with LATEST_LOCK:
        snapshot = dict((code, dict(tick)) for code, tick in LATEST_TICKS.items())
    payload = {
        "schema_version": 1,
        "source": "QMT_INTERNAL_PYTHON",
        "written_at_ms": int(time.time() * 1000),
        "subscription_id": SUBSCRIPTION_ID,
        "ticks": snapshot,
    }
    try:
        if not os.path.isdir(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
        with open(TEMP_FILE, "w") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(TEMP_FILE, OUTPUT_FILE)
    except Exception as exc:
        print("A_AGENT_BRIDGE_FLUSH_ERROR " + type(exc).__name__ + ": " + str(exc))


def init(ContextInfo):
    global SUBSCRIPTION_ID
    if not os.path.isdir(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    SUBSCRIPTION_ID = ContextInfo.subscribe_whole_quote(CODES, callback=on_whole_quote)
    ContextInfo.run_time("flush", "1nSecond", "2020-01-01 00:00:00")
    print("A_AGENT_BRIDGE_STARTED subscription_id=" + str(SUBSCRIPTION_ID))


def handlebar(ContextInfo):
    pass
