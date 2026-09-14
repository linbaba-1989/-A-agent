"""Shared historical-data cache for scanner and stock research."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from .market_cache import history_indicators


HISTORY_STATES = ("not_started", "initializing", "ready", "partial", "failed")


@dataclass(frozen=True)
class HistoryCacheInfo:
    status: str
    total: int
    ready: int
    insufficient: int
    unavailable: int
    failed: int


class HistoricalDataService:
    """Owns one process-wide history view and its derived indicator cache."""

    def __init__(self, provider: Any):
        self.provider = provider
        self.frames: dict[str, pd.DataFrame] = {}
        self._indicator_cache: dict[tuple[str, str | None, int], dict[str, Any]] = {}
        self.status = "not_started"
        self.total = 0
        self.failures: dict[str, str] = {}

    @staticmethod
    def _usable(frame: pd.DataFrame | None, minimum: int = 4) -> bool:
        return isinstance(frame, pd.DataFrame) and len(frame) >= minimum and "close" in frame

    def _store(self, payload: dict[str, pd.DataFrame] | None) -> None:
        for symbol, frame in (payload or {}).items():
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                previous = self.frames.get(symbol)
                if previous is None or len(frame) >= len(previous):
                    self.frames[symbol] = frame
                    for key in [key for key in self._indicator_cache if key[0] == symbol]:
                        self._indicator_cache.pop(key, None)

    def _download_missing(self, symbols: list[str], progress_callback=None) -> None:
        backend = getattr(self.provider, "backend", None)
        if backend is None or not hasattr(backend, "request"):
            return
        batch_size = 100
        for offset in range(0, len(symbols), batch_size):
            batch = symbols[offset:offset + batch_size]
            try:
                backend.request("download_history_data", symbols=batch, period="1d")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                self.failures.update({symbol: reason for symbol in batch})
            if progress_callback:
                done = min(offset + len(batch), len(symbols))
                progress_callback(.35 + .45 * done / max(1, len(symbols)),
                                  f"正在补齐历史日K {done}/{len(symbols)}")

    def initialize(self, symbols: list[str], count: int = 65, progress_callback=None) -> None:
        if self.status in {"ready", "partial"} and self.total == len(symbols):
            return
        self.status, self.total, self.failures = "initializing", len(symbols), {}
        try:
            self._store(self.provider.get_local_history(symbols, count))
            missing = [symbol for symbol in symbols if not self._usable(self.frames.get(symbol))]
            if progress_callback:
                progress_callback(.35, f"本地历史缓存 {len(symbols) - len(missing)}/{len(symbols)}")
            if missing:
                self._download_missing(missing, progress_callback)
                self._store(self.provider.get_local_history(missing, count))
            ready = sum(isinstance(self.frames.get(symbol), pd.DataFrame) and
                        len(self.frames[symbol]) >= 60 and "close" in self.frames[symbol]
                        for symbol in symbols)
            self.status = "ready" if ready == len(symbols) else "partial"
        except Exception as exc:
            self.status = "failed"
            self.failures["*"] = f"{type(exc).__name__}: {exc}"
            raise

    def ensure(self, symbol: str, count: int = 250) -> pd.DataFrame:
        current = self.frames.get(symbol)
        if self._usable(current) and len(current) >= count:
            return current
        self._download_missing([symbol])
        try:
            self._store(self.provider.get_history([symbol], "1d", count))
        except AttributeError:
            self._store(self.provider.get_local_history([symbol], count))
        return self.frames.get(symbol, pd.DataFrame())

    def frame(self, symbol: str) -> pd.DataFrame:
        return self.frames.get(symbol, pd.DataFrame())

    def reason(self, symbol: str, minimum_bars: int = 60) -> str:
        if self.status in {"not_started", "initializing"}:
            return "cache_not_ready"
        frame = self.frames.get(symbol)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return "history_unavailable"
        if len(frame) < minimum_bars:
            return "insufficient_history"
        return "available"

    def indicators(self, symbol: str, as_of: datetime | None) -> dict[str, Any]:
        frame = self.frame(symbol)
        date_key = as_of.date().isoformat() if as_of else None
        key = (symbol, date_key, len(frame))
        if key not in self._indicator_cache:
            self._indicator_cache[key] = history_indicators(frame, as_of)
        return self._indicator_cache[key]

    def info(self) -> HistoryCacheInfo:
        ready = sum(isinstance(frame, pd.DataFrame) and len(frame) >= 60 and "close" in frame
                    for frame in self.frames.values())
        insufficient = sum(isinstance(frame, pd.DataFrame) and 0 < len(frame) < 60
                           for frame in self.frames.values())
        unavailable = max(0, self.total - ready - insufficient)
        return HistoryCacheInfo(self.status, self.total, ready, insufficient, unavailable, len(self.failures))
