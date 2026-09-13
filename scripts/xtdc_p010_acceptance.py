"""P0.10 universe audit and ten cached scanner rounds. Never outputs the token."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scanner import MarketScanner
from src.xtdc_provider import TOKEN_RUNTIME_VERSION, XtDataCenterProvider


def summarize(values):
    ordered = sorted(values)
    return {"average": statistics.mean(values), "p95": ordered[max(0, int(len(ordered) * 0.95 + 0.999) - 1)],
            "maximum": max(values)}


def run(output_path="outputs/diagnostics/xtdc_p010_audit.json"):
    load_dotenv()
    provider = XtDataCenterProvider()
    report = {"runtime_version": TOKEN_RUNTIME_VERSION, "token_configured": provider.configured,
              "token_expiry": "unknown"}
    try:
        report["initialized"] = provider.init()
        report["init_error"] = provider.init_error
        if not report["initialized"]:
            return report
        report["provider"] = "XtDataCenter Token"
        report["audit"] = provider.audit_a_share_universe()
        scanner = MarketScanner(provider)
        scanner.scan(30)  # Initialize history and realtime-indicator caches before measurement.
        rounds = []
        for index in range(10):
            result = scanner.scan(30)
            item = {"round": index + 1, "tick_seconds": result.diagnostics.full_tick_seconds,
                    "indicator_seconds": result.diagnostics.realtime_indicator_seconds,
                    "filter_seconds": result.diagnostics.filter_seconds,
                    "total_seconds": result.diagnostics.elapsed_seconds,
                    "valid_quotes": result.diagnostics.valid_quote_count}
            rounds.append(item)
        report["cached_rounds"] = rounds
        report["cached_summary"] = {
            field: summarize([row[field] for row in rounds])
            for field in ("tick_seconds", "indicator_seconds", "filter_seconds", "total_seconds")
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["output_path"] = str(path)
        return report
    finally:
        provider.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
