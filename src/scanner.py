"""Cached full A-share scanner; only current ticks are read on each scan."""
from __future__ import annotations

import os
from datetime import datetime
from time import perf_counter
from typing import Any

import pandas as pd

from .indicators import UNAVAILABLE, intraday_position, percent_change
from .history_service import HistoricalDataService
from .market_cache import (SnapshotHistory, daily_suspend_flag, history_indicators, infer_volume_unit,
                           normalize_instrument, realtime_ma, security_status, validated_turnover)
from .models import MarketDiagnostics, ScanResult
from .qmt_provider import QMTProvider


def _value(value: Any) -> float | None:
    try:
        return float(value) if value != UNAVAILABLE and pd.notna(value) else None
    except (TypeError, ValueError):
        return None


def calculate_score(row: dict[str, Any]) -> dict[str, float]:
    last = _value(row["lastPrice"])
    averages = [_value(row[key]) for key in ("ma5", "ma10", "ma20")]
    trend = 0.0 if last is None or any(value is None for value in averages) else sum(last > value for value in averages) / 3 * 25
    change, acceleration = _value(row["change_pct"]), _value(row["speed_5m"])
    momentum = 0.0 if change is None else min(max(change, 0) / 7, 1) * 15 + min(max(acceleration or 0, 0) / 2, 1) * 10
    ratio = _value(row["volume_ratio"])
    volume = min(max((ratio or 0) - 0.8, 0) / 1.2, 1) * 20
    high5, high20 = _value(row["high_5d"]), _value(row["high_20d"])
    breakout = 0.0 if last is None else (10 if high5 and last >= high5 else 0) + (15 if high20 and last >= high20 else 0)
    scores = {"score_trend": round(trend, 2), "score_momentum": round(momentum, 2),
              "score_volume": round(volume, 2), "score_breakout": float(breakout), "score_sector": 0.0}
    scores["local_score"] = round(sum(scores.values()), 2)
    return scores


class MarketScanner:
    def __init__(self, provider: QMTProvider, history_service: HistoricalDataService | None = None):
        self.provider = provider
        self.universe: list[str] = []
        self.instrument_cache: dict[str, dict[str, Any]] = {}
        self.history_service = history_service or HistoricalDataService(provider)
        self.history_indicator_cache = self.history_service._indicator_cache
        self.history_frame_cache = self.history_service.frames
        self.suspend_flag_cache: dict[str, Any] = {}
        self.suspend_cache_date = None
        self.snapshot_history = SnapshotHistory(600)
        self.volume_field: str | None = None
        self.volume_multiplier: float | None = None
        self.volume_unit_evidence: list[dict[str, Any]] = []
        self.history_init_seconds = 0.0
        self.tick_strategy = "batch"
        self.benchmark: dict[str, Any] = {}
        self._connection: MarketDiagnostics | None = None

    @property
    def initialized(self) -> bool:
        return bool(self.universe)

    def initialize(self, progress_callback=None) -> None:
        if self.initialized:
            return
        started = perf_counter()
        self.universe = self.provider.get_stock_universe()
        total = len(self.universe)
        for index, code in enumerate(self.universe, 1):
            try:
                self.instrument_cache[code] = normalize_instrument(code, self.provider.get_instrument_detail(code))
            except Exception as exc:
                self.instrument_cache[code] = normalize_instrument(code, {"InstrumentStatus": f"DETAIL_ERROR: {exc!r}"})
            if progress_callback and (index % 100 == 0 or index == total):
                progress_callback(index / total * 0.65, f"正在读取合约信息 {index}/{total}")
        history_progress = (lambda value, message: progress_callback(.65 + .35 * value, message)) if progress_callback else None
        self.history_service.initialize(self.universe, 65, history_progress)
        self.history_frame_cache = self.history_service.frames
        if progress_callback:
            progress_callback(1.0, "历史日K读取完成")
        self.history_init_seconds = perf_counter() - started

    def validate_volume_unit(self, ticks: dict[str, dict[str, Any]]) -> None:
        samples = []
        for code in ("600000.SH", "000001.SZ", "600519.SH"):
            if code in ticks and code in self.instrument_cache:
                samples.append((ticks[code], self.instrument_cache[code]))
        self.volume_field, self.volume_multiplier, self.volume_unit_evidence = infer_volume_unit(samples)

    def compare_tick_strategies(self) -> dict[str, Any]:
        self.initialize()
        result: dict[str, Any] = {}
        for name, loader in (("market", lambda: self.provider.get_market_ticks()),
                             ("batch", lambda: self.provider.get_full_ticks(self.universe))):
            started = perf_counter()
            try:
                ticks = loader()
                filtered = {code: tick for code, tick in ticks.items() if code in set(self.universe)}
                result[name] = {"seconds": perf_counter() - started, "returned": len(ticks),
                                "universe_returned": len(filtered),
                                "valid": sum(self.provider._valid_tick(tick) for tick in filtered.values()), "error": None}
            except Exception as exc:
                result[name] = {"seconds": perf_counter() - started, "returned": 0, "universe_returned": 0,
                                "valid": 0, "error": repr(exc)}
        eligible = [(data["seconds"], name) for name, data in result.items()
                    if not data["error"] and data["universe_returned"] >= len(self.universe) * 0.95]
        if eligible:
            self.tick_strategy = min(eligible)[1]
        result["selected"] = self.tick_strategy
        self.benchmark = result
        return result

    def _ticks(self) -> dict[str, dict[str, Any]]:
        if self.tick_strategy == "market":
            all_ticks = self.provider.get_market_ticks()
            universe = set(self.universe)
            return {code: tick for code, tick in all_ticks.items() if code in universe}
        return self.provider.get_full_ticks(self.universe)

    @staticmethod
    def _volume_ratio(tick: dict[str, Any], history: dict[str, Any]):
        try:
            baseline = float(history["avg_volume5"])
            return round(float(tick["volume"]) / baseline, 4) if baseline > 0 else UNAVAILABLE
        except (KeyError, TypeError, ValueError):
            return UNAVAILABLE

    def _tick_vwap(self, tick: dict[str, Any]):
        if not self.volume_field or not self.volume_multiplier:
            return UNAVAILABLE
        try:
            shares = float(tick[self.volume_field]) * self.volume_multiplier
            return round(float(tick["amount"]) / shares, 4) if shares > 0 else UNAVAILABLE
        except (KeyError, TypeError, ValueError):
            return UNAVAILABLE

    @staticmethod
    def _amplitude(tick: dict[str, Any]):
        try:
            previous = float(tick["lastClose"])
            return round((float(tick["high"]) - float(tick["low"])) / previous * 100, 4) if previous > 0 else UNAVAILABLE
        except (KeyError, TypeError, ValueError):
            return UNAVAILABLE

    def _suspend_flag(self, symbol: str, as_of: datetime | None):
        trading_date = as_of.date() if as_of else None
        if trading_date != self.suspend_cache_date:
            self.suspend_flag_cache.clear()
            self.suspend_cache_date = trading_date
        if symbol not in self.suspend_flag_cache:
            self.suspend_flag_cache[symbol] = daily_suspend_flag(
                self.history_frame_cache.get(symbol, pd.DataFrame()), as_of
            )
        return self.suspend_flag_cache[symbol]

    def scan(self, top_n: int = 30, progress_callback=None) -> ScanResult:
        started = perf_counter()
        connection = self._connection or self.provider.connection_diagnostics()
        if not connection.connected:
            return ScanResult([], connection)
        self._connection = connection
        self.initialize(progress_callback)
        tick_started = perf_counter()
        ticks = self._ticks()
        full_tick_seconds = perf_counter() - tick_started
        if self.volume_field is None:
            self.validate_volume_unit(ticks)
        counts = {"suspended": 0, "resumed_today": 0, "unknown": 0, "invalid_quote": 0}
        rows: list[dict[str, Any]] = []
        indicator_started = perf_counter()
        for symbol in self.universe:
            tick = ticks.get(symbol)
            instrument = self.instrument_cache.get(symbol, {})
            timestamp = self.provider.tick_timestamp(tick) if tick else None
            as_of = datetime.fromtimestamp(timestamp) if timestamp else None
            suspend_flag = self._suspend_flag(symbol, as_of)
            state = security_status(tick, suspend_flag)
            if state in {"suspended", "invalid_quote"}:
                counts[state] = counts.get(state, 0) + 1
                continue
            assert tick is not None
            self.snapshot_history.update(symbol, timestamp, tick)
            history = self.history_service.indicators(symbol, as_of)
            last = tick.get("lastPrice")
            closes = history.get("closes", [])
            turnover = validated_turnover(tick, instrument)
            row: dict[str, Any] = {
                "symbol": symbol, "name": instrument.get("name", ""), "quote_time": self.provider.normalize_tick(symbol, tick)["timestamp"],
                "lastPrice": last, "lastClose": tick.get("lastClose", UNAVAILABLE),
                "change_pct": percent_change(last, tick.get("lastClose")), "amount": tick.get("amount", UNAVAILABLE),
                "volume": tick.get("volume", UNAVAILABLE), "float_volume": instrument.get("float_volume") or UNAVAILABLE,
                "turnover_rate_raw": turnover["value"], "turnover_rate": turnover["value"],
                "turnover_status": turnover["status"],
                "turnover_by_volume": turnover["volume_formula"],
                "turnover_by_pvolume": turnover["pvolume_formula"],
                "amplitude": self._amplitude(tick), "vwap": self._tick_vwap(tick),
                "intraday_position": intraday_position(last, tick.get("low"), tick.get("high")),
                "speed_1m": self.snapshot_history.speed(symbol, 1), "speed_3m": self.snapshot_history.speed(symbol, 3),
                "speed_5m": self.snapshot_history.speed(symbol, 5), "ma5": realtime_ma(closes, last, 5),
                "ma10": realtime_ma(closes, last, 10), "ma20": realtime_ma(closes, last, 20),
                "ma60": realtime_ma(closes, last, 60), "previous_ma5": history.get("previous_ma5", UNAVAILABLE),
                "previous_ma10": history.get("previous_ma10", UNAVAILABLE), "previous_ma20": history.get("previous_ma20", UNAVAILABLE),
                "previous_ma60": history.get("previous_ma60", UNAVAILABLE), "previous_high": history.get("high_1d", UNAVAILABLE),
                "high_5d": history.get("high_5d", UNAVAILABLE), "high_10d": history.get("high_10d", UNAVAILABLE),
                "high_20d": history.get("high_20d", UNAVAILABLE), "atr14": history.get("atr14", UNAVAILABLE),
                "volume_ratio": self._volume_ratio(tick, history), "security_status": state,
                "history_status": self.history_service.reason(symbol),
                "suspendFlag": suspend_flag, "stockStatus": tick.get("stockStatus"), "openInt": tick.get("openInt"),
                "instrument_status": instrument.get("instrument_status"), "is_trading": instrument.get("is_trading"),
                "source": "QMT/xtquant",
            }
            row.update(calculate_score(row))
            rows.append(row)
        realtime_indicator_seconds = perf_counter() - indicator_started
        filter_started = perf_counter()
        rows.sort(key=lambda item: item["local_score"], reverse=True)
        filter_seconds = perf_counter() - filter_started
        try:
            import psutil
            memory_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        except Exception:
            memory_mb = None
        diagnostic = MarketDiagnostics(
            connected=True, xtquant_path=connection.xtquant_path, xtquant_imported=True, rpc_request_success=True,
            full_tick_success=True, process_detected=connection.process_detected, processes=connection.processes,
            python_version=connection.python_version, stock_pool_size=len(self.universe),
            sh_count=sum(code.endswith(".SH") for code in self.universe), sz_count=sum(code.endswith(".SZ") for code in self.universe),
            bj_count=sum(code.endswith(".BJ") for code in self.universe), full_tick_count=len(ticks), valid_quote_count=len(rows),
            suspended_count=counts["suspended"], abnormal_count=counts["unknown"],
            invalid_quote_count=counts["invalid_quote"],
            turnover_valid_count=sum(row["turnover_rate"] != UNAVAILABLE for row in rows),
            speed_1m_valid_count=sum(row["speed_1m"] != UNAVAILABLE for row in rows),
            speed_3m_valid_count=sum(row["speed_3m"] != UNAVAILABLE for row in rows),
            speed_5m_valid_count=sum(row["speed_5m"] != UNAVAILABLE for row in rows),
            full_tick_seconds=full_tick_seconds, history_init_seconds=self.history_init_seconds,
            realtime_indicator_seconds=realtime_indicator_seconds, filter_seconds=filter_seconds,
            memory_mb=memory_mb, elapsed_seconds=perf_counter() - started, message="全 A 实时筛选完成",
        )
        unavailable = [] if diagnostic.turnover_valid_count else ["turnover_rate（双公式交叉验证未通过）"]
        unavailable.append("sector_strength")
        return ScanResult(rows[:top_n], diagnostic, unavailable)
