"""Read-only P0 acceptance during a live continuous-auction session."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from dotenv import load_dotenv
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.market_cache import SnapshotHistory, validated_turnover  # noqa: E402
from src.qmt_provider import QMTProvider  # noqa: E402
from src.scanner import MarketScanner  # noqa: E402

DURATION_SECONDS = 600
INTERVAL_SECONDS = 10
FIXED_CODES = ["600000.SH", "000001.SZ", "600519.SH"]
OUTPUT = Path(__file__).resolve().parents[1] / "outputs" / "p0_trading_session_acceptance.json"


def require_live_tick(provider: QMTProvider, ticks: dict[str, dict]) -> datetime:
    timestamps = [provider.tick_timestamp(tick) for tick in ticks.values()]
    timestamps = [value for value in timestamps if value]
    if not timestamps:
        raise RuntimeError("live_tick_time_missing")
    moment = datetime.fromtimestamp(max(timestamps))
    if moment.date() != datetime.now().date():
        raise RuntimeError(f"not_current_trading_day: tick={moment.isoformat()}")
    if SnapshotHistory.trading_session(moment.timestamp()) is None:
        raise RuntimeError(f"not_continuous_auction: tick={moment.isoformat()}")
    return moment


def quote_fields(tick: dict) -> dict:
    last, previous = float(tick["lastPrice"]), float(tick["lastClose"])
    tick_time = QMTProvider.tick_timestamp(tick)
    return {"tick_time": datetime.fromtimestamp(tick_time).isoformat(timespec="seconds"),
            "lastPrice": last, "lastClose": previous, "change_pct": round((last / previous - 1) * 100, 6),
            "high": tick.get("high"), "low": tick.get("low"), "volume": tick.get("volume"),
            "pvolume": tick.get("pvolume"), "amount": tick.get("amount")}


if __name__ == "__main__":
    load_dotenv()
    provider = QMTProvider()
    scanner = MarketScanner(provider)
    scanner.initialize()
    require_live_tick(provider, provider.get_full_ticks(FIXED_CODES))
    process = psutil.Process()
    process.cpu_percent(None)
    started = time.monotonic()
    timeline, latest = [], None
    while time.monotonic() - started < DURATION_SECONDS:
        latest = scanner.scan(top_n=len(scanner.universe))
        require_live_tick(provider, provider.get_full_ticks(FIXED_CODES))
        timeline.append({"at": datetime.now().isoformat(), "elapsed_seconds": round(time.monotonic() - started, 3),
                         "speed_1m_valid": latest.diagnostics.speed_1m_valid_count,
                         "speed_3m_valid": latest.diagnostics.speed_3m_valid_count,
                         "speed_5m_valid": latest.diagnostics.speed_5m_valid_count,
                         "tick_seconds": latest.diagnostics.full_tick_seconds,
                         "indicator_seconds": latest.diagnostics.realtime_indicator_seconds,
                         "filter_seconds": latest.diagnostics.filter_seconds,
                         "total_seconds": latest.diagnostics.elapsed_seconds,
                         "cpu_percent": process.cpu_percent(None),
                         "memory_mb": process.memory_info().rss / 1024 / 1024})
        print(json.dumps(timeline[-1], ensure_ascii=False), flush=True)
        time.sleep(INTERVAL_SECONDS)
    active = [row["symbol"] for row in sorted(latest.rows, key=lambda row: float(row.get("amount") or 0), reverse=True)
              if row["symbol"] not in FIXED_CODES][:2]
    selected = FIXED_CODES + active
    final_ticks = provider.get_full_ticks(selected)
    comparisons = []
    for code in selected:
        instrument = scanner.instrument_cache[code]
        comparisons.append({"code": code, "name": instrument.get("name"), **quote_fields(final_ticks[code]),
                            "FloatVolume": instrument.get("float_volume"), "TotalVolume": instrument.get("total_volume"),
                            "turnover": validated_turnover(final_ticks[code], instrument),
                            "speeds": {f"{minutes}m": scanner.snapshot_history.speed_details(code, minutes)
                                       for minutes in (1, 3, 5)}})
    report = {"runtime": provider.runtime_info(), "duration_seconds": time.monotonic() - started,
              "final_diagnostics": latest.diagnostics.to_dict(), "timeline": timeline, "five_stocks": comparisons,
              "boundary_assertions": {"cross_day": "live date gate plus per-session cache clear",
                                      "cross_lunch": "morning/afternoon session key resets cache",
                                      "post_auction": "cache starts at 09:30"},
              "speed_formula": "(current / previous - 1) * 100",
              "gui_values": "pending_same-time_manual_QMT_GUI_capture"}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved": str(OUTPUT), "duration_seconds": report["duration_seconds"]}, ensure_ascii=False, indent=2))
