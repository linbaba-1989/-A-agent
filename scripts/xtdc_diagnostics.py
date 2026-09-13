"""Real XtDataCenter diagnostics. Never prints the token."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from time import perf_counter

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.market_cache import normalize_instrument
from src.xtdc_provider import TOKEN_RUNTIME_VERSION, XtDataCenterProvider

CODES = ["600000.SH", "000001.SZ", "600498.SH"]
CAPITAL_CODES = ["000001.SZ", "600000.SH", "600519.SH"]


def valid_tick(tick):
    try:
        return float(tick.get("lastPrice") or 0) > 0 and float(tick.get("lastClose") or 0) > 0
    except (AttributeError, TypeError, ValueError):
        return False


def run():
    load_dotenv()
    provider = XtDataCenterProvider()
    report = {"runtime_version": TOKEN_RUNTIME_VERSION, "token_configured": provider.configured,
              "token_expiry": "unknown", "initialized": False}
    try:
        started = perf_counter()
        report["initialized"] = provider.init()
        report["init_seconds"] = perf_counter() - started
        report["init_error"] = provider.init_error
        report.update(provider.runtime_info)
        if not report["initialized"]:
            return report
        sectors = provider.backend.request("get_sector_list")
        report["sector_list_count"] = len(sectors) if isinstance(sectors, list) else 0
        report["quote_server_status"] = provider.backend.request("get_quote_server_status")
        ticks = provider.get_full_ticks(CODES)
        if not all(valid_tick(ticks.get(code)) for code in CODES):
            time.sleep(8)
            ticks = provider.get_full_ticks(CODES)
        report["three_stock_ticks"] = {code: ticks.get(code) for code in CODES}
        report["three_stock_success"] = all(valid_tick(ticks.get(code)) for code in CODES)
        report["capital"] = {}
        for code in CAPITAL_CODES:
            detail = provider.get_instrument_detail(code)
            normalized = normalize_instrument(code, detail)
            report["capital"][code] = {"FloatVolume": detail.get("FloatVolume"),
                                       "TotalVolume": detail.get("TotalVolume"),
                                       "valid": bool(normalized["float_volume"] and normalized["total_volume"])}
        report["history"] = {}
        for period in ("1d", "1m"):
            provider.backend.request("download_history_data", symbols=CODES, period=period)
            frames = provider.get_local_history(CODES, count=65, period=period)
            report["history"][period] = {code: len(frames.get(code, [])) for code in CODES}
        universe_started = perf_counter()
        try:
            universe = provider.get_stock_universe()
            report["universe"] = {"total": len(universe), "SH": sum(code.endswith(".SH") for code in universe),
                                  "SZ": sum(code.endswith(".SZ") for code in universe),
                                  "BJ": sum(code.endswith(".BJ") for code in universe),
                                  "seconds": perf_counter() - universe_started, "error": None}
        except Exception as exc:
            report["universe"] = {"total": 0, "SH": 0, "SZ": 0, "BJ": 0,
                                  "seconds": perf_counter() - universe_started,
                                  "error": type(exc).__name__ + ": " + str(exc)}
            return report
        batch_started = perf_counter()
        batch = provider.get_full_ticks(universe)
        report["batch_ticks"] = {"returned": len(batch), "valid": sum(valid_tick(tick) for tick in batch.values()),
                                 "seconds": perf_counter() - batch_started}
        market_started = perf_counter()
        try:
            market = provider.get_market_ticks()
            report["market_ticks"] = {"returned": len(market), "valid": sum(valid_tick(tick) for tick in market.values()),
                                      "seconds": perf_counter() - market_started, "error": None}
        except Exception as exc:
            report["market_ticks"] = {"returned": 0, "valid": 0, "seconds": perf_counter() - market_started,
                                      "error": type(exc).__name__ + ": " + str(exc)}
        return report
    finally:
        provider.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
