"""Isolated official XtDataCenter Token market-data provider."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable

import pandas as pd

from .market_cache import normalize_instrument
from .market_universe_audit import classify_missing_symbol, market_of, summarize_missing
from .models import MarketDiagnostics, ProviderStatus

TOKEN_RUNTIME_VERSION = "250807.1.2"
TOKEN_RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "tools" / "xtquant_runtime" / "token"
WORKER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "xtdc_worker.py"
QUOTE_FIELDS = ("lastPrice", "open", "high", "low", "lastClose", "volume", "pvolume", "amount", "stockStatus")


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


class SubprocessXtDataCenterBackend:
    def __init__(self, runtime_root: Path = TOKEN_RUNTIME_ROOT, timeout: float = 90.0):
        self.runtime_root = runtime_root
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self._counter = 0
        self._lock = threading.Lock()

    def start(self, token: str) -> dict[str, Any]:
        package = self.runtime_root / "xtquant"
        if not (package / "xtdatacenter.py").is_file() or not list(package.glob("*.pyd")):
            raise RuntimeError("xtdc_runtime_incomplete")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(WORKER_PATH), str(self.runtime_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        return self.request("init", token=token, version=TOKEN_RUNTIME_VERSION,
                            data_home=str(self.runtime_root / "data"))

    def request(self, command: str, **payload: Any) -> Any:
        with self._lock:
            if not self.process or not self.process.stdin or not self.process.stdout:
                raise RuntimeError("xtdc_worker_not_started")
            self._counter += 1
            request_id = self._counter
            self.process.stdin.write(json.dumps({"id": request_id, "command": command, **payload}, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = None
            while response is None:
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("xtdc_worker_terminated")
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("id") == request_id:
                    response = candidate
            if not response.get("success"):
                raise RuntimeError(response.get("error") or "xtdc_worker_error")
            return response.get("result")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.request("close")
            except Exception:
                self.process.terminate()
        self.process = None


class XtDataCenterProvider:
    provider_name = "XtDataCenter Token"

    def __init__(self, token: str | None = None, backend_factory: Callable[[], Any] | None = None):
        self.token = token if token is not None else os.getenv("XTDC_TOKEN", "")
        self.backend_factory = backend_factory or SubprocessXtDataCenterBackend
        self.backend: Any = None
        self.runtime_info: dict[str, Any] = {}
        self.init_error: str | None = None
        self._raw_universe: list[str] | None = None
        self._active_universe: list[str] | None = None
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self.universe_audit: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token.strip())

    def init(self) -> bool:
        if not self.configured:
            self.init_error = "xtdc_token_missing"
            return False
        try:
            self.backend = self.backend_factory()
            self.runtime_info = self.backend.start(self.token)
            self.init_error = None
            return True
        except Exception as exc:
            self.init_error = f"{type(exc).__name__}: {exc}".replace(self.token, "***")
            self.close()
            return False

    def check_connection(self) -> ProviderStatus:
        if self.backend is None and not self.init():
            return ProviderStatus(False, self.init_error or "xtdc_init_init_failed")
        try:
            sectors = self.backend.request("get_sector_list")
            return ProviderStatus(isinstance(sectors, list), "xtdc_connected" if isinstance(sectors, list) else "xtdc_sector_invalid",
                                  {"runtime": TOKEN_RUNTIME_VERSION, "token_configured": self.configured})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}".replace(self.token, "***")
            return ProviderStatus(False, error, {"runtime": TOKEN_RUNTIME_VERSION, "token_configured": self.configured})

    def status(self) -> ProviderStatus:
        return self.check_connection()

    def connection_diagnostics(self) -> MarketDiagnostics:
        status = self.check_connection()
        return MarketDiagnostics(connected=status.ok, xtquant_path=self.runtime_info.get("xtdata_path"),
                                 xtquant_imported=bool(self.runtime_info), rpc_request_success=status.ok,
                                 message=status.message)

    @staticmethod
    def _is_a_share(code: str) -> bool:
        if code.endswith(".SH"):
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if code.endswith(".SZ"):
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        return code.endswith(".BJ") and code.startswith(("4", "8", "9"))

    def get_raw_a_share_universe(self) -> list[str]:
        if self._raw_universe is not None:
            return list(self._raw_universe)
        stocks: list[str] = []
        for sector in ("沪深京A股", "沪深A股", "沪A", "深A", "北交所", "京A"):
            try:
                stocks.extend(self.backend.request("get_stock_list_in_sector", sector=sector) or [])
            except Exception:
                continue
        result = sorted({code for code in stocks if self._is_a_share(code)})
        if not result:
            self.backend.request("download_sector_data")
            for sector in ("沪深京A股", "沪深A股", "沪A", "深A", "北交所", "京A"):
                try:
                    stocks.extend(self.backend.request("get_stock_list_in_sector", sector=sector) or [])
                except Exception:
                    continue
            result = sorted({code for code in stocks if self._is_a_share(code)})
        if not result:
            stocks.extend(self.backend.request("get_all_sector_stocks") or [])
            result = sorted({code for code in stocks if self._is_a_share(code)})
        if not result:
            raise RuntimeError("xtdc_a_share_universe_empty_after_sector_download")
        self._raw_universe = result
        return list(result)

    @staticmethod
    def _valid_instrument(detail: dict[str, Any] | None) -> bool:
        return bool(detail and (detail.get("InstrumentName") or detail.get("name")) and
                    (detail.get("ExchangeID") or detail.get("exchangeID") or detail.get("Market")))

    def audit_a_share_universe(self) -> dict[str, Any]:
        if self.universe_audit is not None:
            return self.universe_audit
        raw = self.get_raw_a_share_universe()
        ticks = self.get_full_ticks(raw)
        tick_symbols = set(ticks)
        missing_symbols = sorted(set(raw) - tick_symbols)
        quote_status = self.backend.request("get_quote_server_status")
        quote_markets = {market for market in ("SH", "SZ", "BJ")
                         if any(f"_{market}_" in key for key in quote_status)}
        missing_rows = []
        for code in missing_symbols:
            try:
                detail = self.get_instrument_detail(code)
            except Exception:
                detail = {}
            reason, evidence = classify_missing_symbol(code, detail, self._is_a_share,
                                                       quote_markets=quote_markets)
            missing_rows.append({"code": code, "reason": reason, "evidence": evidence})
        active = []
        invalid_returned = []
        for code in raw:
            tick = ticks.get(code)
            if not self._valid_tick(tick, require_time=True):
                if code in tick_symbols:
                    invalid_returned.append(code)
                continue
            try:
                detail = self.get_instrument_detail(code)
            except Exception:
                detail = {}
            if self._valid_instrument(detail):
                active.append(code)
            else:
                invalid_returned.append(code)
        timestamps = [self.tick_timestamp(tick) for tick in ticks.values()]
        timestamps = [value for value in timestamps if value is not None]
        latest_seconds = max(timestamps) if timestamps else None
        latest_quote_time = datetime.fromtimestamp(latest_seconds).isoformat(timespec="seconds") if latest_seconds else "unavailable"
        quote_moment = datetime.fromtimestamp(latest_seconds) if latest_seconds else None
        current = datetime.now()
        market_open = bool(quote_moment and quote_moment.date() == current.date() and current.weekday() < 5 and
                           ((9, 30) <= (quote_moment.hour, quote_moment.minute) <= (11, 30) or
                            (13, 0) <= (quote_moment.hour, quote_moment.minute) <= (15, 0)))
        by_market = {}
        for market in ("SH", "SZ", "BJ"):
            market_raw = [code for code in raw if market_of(code) == market]
            market_ticks = [code for code in tick_symbols if market_of(code) == market]
            market_valid = [code for code in active if market_of(code) == market]
            by_market[market] = {"raw": len(market_raw), "tick_returned": len(market_ticks),
                                 "valid": len(market_valid), "missing": len(set(market_raw) - tick_symbols)}
        bj_active = [code for code in active if code.endswith(".BJ")]
        bj_sample = []
        if bj_active:
            step = max(1, len(bj_active) // 10)
            for code in bj_active[::step][:10]:
                tick, detail = ticks[code], self._instrument_cache.get(code, {})
                bj_sample.append({"code": code, "name": detail.get("InstrumentName"),
                                  "lastPrice": tick.get("lastPrice"), "lastClose": tick.get("lastClose"),
                                  "InstrumentStatus": detail.get("InstrumentStatus")})
        self._active_universe = sorted(active)
        self.universe_audit = {
            "raw_count": len(raw), "active_count": len(active), "tick_returned_count": len(ticks),
            "valid_tick_count": sum(self._valid_tick(tick, True) for tick in ticks.values()),
            "missing_tick_count": len(missing_symbols), "missing_tick_symbols": missing_symbols,
            "latest_quote_time": latest_quote_time, "market_status": "open" if market_open else "closed",
            "missing_tick_first_100": missing_symbols[:100], "missing_by_market": by_market,
            "missing_reason_counts": summarize_missing(missing_rows), "missing_classification": missing_rows,
            "invalid_returned_symbols": sorted(set(invalid_returned)), "bj_sample": bj_sample,
        }
        return self.universe_audit

    def get_stock_universe(self) -> list[str]:
        if self._active_universe is None:
            self.audit_a_share_universe()
        return list(self._active_universe or [])

    def get_a_share_universe(self) -> list[str]:
        return self.get_stock_universe()

    def get_full_ticks(self, symbols: list[str], batch_size: int = 500) -> dict[str, dict[str, Any]]:
        expected = len(symbols)
        result: dict[str, dict[str, Any]] = {}
        for attempt in range(4):
            result = {}
            for batch in _chunks(symbols, batch_size):
                payload = self.backend.request("get_full_tick", symbols=batch)
                if not isinstance(payload, dict):
                    raise RuntimeError("xtdc_full_tick_invalid")
                result.update({code: tick for code, tick in payload.items() if isinstance(tick, dict)})
            if not expected or len(result) >= expected * 0.9 or attempt == 3:
                return result
            time.sleep(2)
        return result

    def get_market_ticks(self, markets: list[str] | None = None) -> dict[str, dict[str, Any]]:
        payload = self.backend.request("get_full_tick", symbols=markets or ["SH", "SZ", "BJ"])
        if not isinstance(payload, dict):
            raise RuntimeError("xtdc_market_tick_invalid")
        return {code: tick for code, tick in payload.items() if isinstance(tick, dict)}

    def get_full_market_ticks(self) -> dict[str, dict[str, Any]]:
        return self.get_market_ticks()

    def get_instrument_detail(self, symbol: str) -> dict[str, Any]:
        if symbol in self._instrument_cache:
            return dict(self._instrument_cache[symbol])
        payload = self.backend.request("get_instrument_detail", symbol=symbol, complete=False)
        if not isinstance(payload, dict):
            raise RuntimeError("xtdc_instrument_invalid")
        self._instrument_cache[symbol] = dict(payload)
        return dict(payload)

    def get_local_history(self, symbols: list[str], count: int = 61, batch_size: int = 300,
                          period: str = "1d") -> dict[str, pd.DataFrame]:
        fields = ["time", "open", "high", "low", "close", "volume", "amount", "suspendFlag"]
        result: dict[str, pd.DataFrame] = {}
        for batch in _chunks(symbols, batch_size):
            payload = self.backend.request("get_local_data", fields=fields, symbols=batch, period=period, count=count)
            for code, encoded in (payload or {}).items():
                result[code] = pd.DataFrame(encoded["data"], columns=encoded["columns"], index=encoded["index"])
        return result

    def get_history(self, symbols: list[str], period: str, count: int, fields=None, batch_size: int = 300):
        return self.get_local_history(symbols, count, batch_size, period)

    @staticmethod
    def _history_range(period: str, start_time: str, end_time: str) -> None:
        if period not in {"1d", "1m", "5m", "15m", "30m"}:
            raise ValueError("unsupported_history_period")
        def parse(value):
            if not isinstance(value, str) or len(value) not in {8, 14} or not value.isdigit():
                raise ValueError("explicit_history_range_required")
            return datetime.strptime(value, "%Y%m%d" if len(value) == 8 else "%Y%m%d%H%M%S")
        start, end = parse(start_time), parse(end_time)
        if start > end or (end - start).total_seconds() > 366 * 86400:
            raise ValueError("invalid_or_over_one_year_history_range")

    def download_history_range(self, symbols: list[str], period: str, start_time: str, end_time: str):
        """Explicit bounded opt-in download; existing production callers are unchanged."""
        self._history_range(period, start_time, end_time)
        if not symbols or len(symbols) > 50:
            raise ValueError("history_download_requires_1_to_50_symbols")
        # This runtime downloads base bars; its reader produces 15m/30m bars.
        # Direct 15m/30m downloads fail even though the SDK reader supports them.
        download_period = "5m" if period in {"15m", "30m"} else period
        return self.backend.request("download_history_data2", symbols=list(dict.fromkeys(symbols)),
                                    period=download_period, start_time=start_time, end_time=end_time)

    def get_history_range(self, symbols: list[str], period: str, start_time: str, end_time: str,
                          *, adjustment: str = "raw", count: int = -1) -> dict[str, pd.DataFrame]:
        """Read one explicit adjustment series without filling missing bars."""
        self._history_range(period, start_time, end_time)
        adjustments = {"raw": "none", "qfq": "front", "hfq": "back"}
        if adjustment not in adjustments:
            raise ValueError("unsupported_history_adjustment")
        if not symbols or len(symbols) > 50 or type(count) is not int or count == 0 or count < -1:
            raise ValueError("invalid_history_symbols_or_count")
        payload = self.backend.request("get_market_data_ex", fields=["time", "open", "high", "low", "close", "volume", "amount"],
            symbols=list(dict.fromkeys(symbols)), period=period, start_time=start_time, end_time=end_time,
            count=count, dividend_type=adjustments[adjustment])
        result = {}
        for symbol, encoded in (payload or {}).items():
            frame = pd.DataFrame(encoded["data"], columns=encoded["columns"], index=encoded["index"])
            if frame.empty and "time" not in frame:
                frame["time"] = pd.Series(index=frame.index, dtype="float64")
            timestamps = pd.to_datetime(pd.to_numeric(frame["time"], errors="coerce"),
                                        unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
            frame["source"], frame["provider"] = "xtdc", self.provider_name
            frame["trade_date"] = timestamps.dt.strftime("%Y-%m-%d")
            frame["quote_time"] = timestamps.map(lambda value: value.isoformat() if pd.notna(value) else None)
            frame["status"], frame["adjustment"] = "HISTORICAL", adjustment
            frame.attrs.update(source="xtdc", provider=self.provider_name, adjustment=adjustment,
                               period=period, status="AVAILABLE" if not frame.empty else "UNAVAILABLE",
                               base_period="5m" if period in {"15m", "30m"} else period)
            result[symbol] = frame
        return result

    @staticmethod
    def tick_timestamp(tick: dict[str, Any]) -> float | None:
        value = tick.get("time") or tick.get("timetag")
        if not isinstance(value, (int, float)) or value <= 0:
            return None
        return value / 1000 if value > 10_000_000_000 else float(value)

    @staticmethod
    def _valid_tick(tick: dict[str, Any] | None, require_time: bool = False) -> bool:
        if not tick:
            return False
        try:
            valid = float(tick.get("lastPrice") or 0) > 0 and float(tick.get("lastClose") or 0) > 0
            return valid and (not require_time or bool(tick.get("time") or tick.get("timetag")))
        except (TypeError, ValueError):
            return False

    @classmethod
    def normalize_tick(cls, symbol: str, tick: dict[str, Any]) -> dict[str, Any]:
        seconds = cls.tick_timestamp(tick)
        timestamp = datetime.fromtimestamp(seconds).isoformat(timespec="seconds") if seconds else "unavailable"
        return {"symbol": symbol, **{field: tick.get(field, "unavailable") for field in QUOTE_FIELDS},
                "timestamp": timestamp, "source": "XtDataCenter Token/xtdata.get_full_tick"}

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        tick = self.get_full_ticks([symbol]).get(symbol)
        return self.normalize_tick(symbol, tick) if self._valid_tick(tick) else None

    def normalized_instrument(self, symbol: str) -> dict[str, Any]:
        return normalize_instrument(symbol, self.get_instrument_detail(symbol))

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
        self.backend = None
