"""Lazy, independently bounded daily SDK adapters."""
from datetime import date
from pathlib import Path
from time import perf_counter
import json
import subprocess
import sys
from ..contracts import canonical_symbol
from ..historical import ADJUSTMENTS, INDEX_SYMBOLS, HistoryResult, validate
from .history_parsers import parse_rows


def sdk_loader(task):
    completed = subprocess.run([sys.executable, "-m", "src.market_data.adapters.history_worker"],
        input=json.dumps(task), capture_output=True, text=True, encoding="utf-8",
        cwd=Path(__file__).resolve().parents[3], timeout=45)
    payload = json.loads(completed.stdout)
    if completed.returncode or "error" in payload:
        # SDK errors may contain URLs; public diagnostics retain category only.
        raise RuntimeError(payload.get("error", "sdk_failed").split(":")[0])
    return payload


class DailyProvider:
    name = ""
    supported_exchanges = frozenset()

    def __init__(self, loader=sdk_loader, clock=None, endpoint="sina"):
        self.loader, self.clock, self.endpoint = loader, clock, endpoint
        if endpoint not in {"em", "sina"}:
            raise ValueError("invalid_akshare_endpoint")

    def supports(self, symbol, kind="equity"):
        return symbol[-2:] in self.supported_exchanges and (kind != "index" or symbol in INDEX_SYMBOLS)

    def daily(self, symbol, start, end, adjustment="raw", kind="equity"):
        symbol = canonical_symbol(symbol)
        if adjustment not in ADJUSTMENTS or kind not in {"equity", "index"}:
            raise ValueError("invalid_history_request")
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise ValueError("invalid_date_range")
        result = HistoryResult(symbol, source=self.name, adjustment=adjustment, requested_end=end)
        if not self.supports(symbol, kind) or (kind == "index" and adjustment != "raw"):
            result.quality_status = "UNSUPPORTED"
            result.warnings = ["unsupported_market_or_index_adjustment"]
            return result
        started = perf_counter()
        try:
            payload = self.loader(dict(provider=self.name, symbol=symbol, start=str(start),
                end=str(end), adjustment=adjustment, kind=kind, endpoint=self.endpoint))
            bars, errors = parse_rows(payload["rows"], symbol, self.name, adjustment, payload["flavor"])
            result = validate(bars, symbol, self.name, adjustment, start, end,
                              now=self.clock() if self.clock else None)
            if errors:
                result.quality_status = "DEGRADED" if result.bars else "UNAVAILABLE"
                result.data_status = "PARTIAL" if result.bars else "UNAVAILABLE"
                result.warnings.extend(errors)
            if payload["flavor"] == "akshare_index_sina":
                result.warnings.append("index_volume_unit_unverified:null")
            result.warnings.append("endpoint:" + payload["flavor"])
        except Exception as exc:
            result.warnings.append("fetch_failed:" + type(exc).__name__)
        result.latency = perf_counter() - started
        return result

    def batch(self, symbols, start, end, adjustment="raw", kind="equity"):
        return {s: self.daily(s, start, end, adjustment, kind) for s in dict.fromkeys(symbols)}


class AKShareHistoryProvider(DailyProvider):
    name = "akshare"
    supported_exchanges = frozenset({"SH", "SZ", "BJ"})


class BaoStockHistoryProvider(DailyProvider):
    name = "baostock"
    supported_exchanges = frozenset({"SH", "SZ"})
