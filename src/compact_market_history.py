"""Time-bounded price history in numeric matrices, with no retained point objects.

Exchange timestamps differ between symbols, including unchanged/late ticks. A
single timestamp per polling frame would change the speed reference. Two float64
matrices therefore keep the exact per-symbol timestamp and price. Each indexed
symbol owns a logical ring in these shared allocations, not a Python deque.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import math
from typing import Iterable

import numpy as np

from .indicators import UNAVAILABLE
from .market_clock import timestamp_to_beijing


@dataclass(frozen=True)
class RealtimePoint:
    """Compatibility view, materialized only when a caller requests points()."""

    timestamp: float
    last_price: float
    volume: float | None = None
    amount: float | None = None
    turnover: float | None = None


class CompactMarketHistory:
    """Retain 5 minutes + margin and one predecessor at the retention boundary.

    ``maxlen`` is a legacy constructor argument, now an initial allocation hint,
    never a sample-count limit. Rings grow if faster sampling needs more space.
    ``speed`` retains the existing API: its window argument is in *minutes*.
    Synchronization is provided by the owning feed, as with the old buffer.
    """

    def __init__(self, maxlen: int = 300, *, retention_seconds: float = 360):
        if not math.isfinite(retention_seconds) or retention_seconds < 300:
            raise ValueError("history must cover at least five minutes")
        if maxlen < 1:
            raise ValueError("initial capacity must be positive")
        self.retention_seconds = float(retention_seconds)
        self._capacity = max(8, int(maxlen))
        self._index: dict[str, int] = {}
        self._sessions: list[tuple[str, str] | None] = []
        self._timestamps = np.empty((0, self._capacity), dtype=np.float64)
        self._prices = np.empty((0, self._capacity), dtype=np.float64)
        self._starts = np.zeros(0, dtype=np.int64)
        self._counts = np.zeros(0, dtype=np.int64)

    @staticmethod
    def trading_session(timestamp: float) -> tuple[str, str] | None:
        moment = timestamp_to_beijing(timestamp)
        if moment is None:
            return None
        clock = moment.time()
        if time(9, 30) <= clock <= time(11, 30):
            return moment.date().isoformat(), "morning"
        if time(13, 0) <= clock <= time(15, 0):
            return moment.date().isoformat(), "afternoon"
        return None

    def reserve_symbols(self, symbols: Iterable[str]) -> None:
        """Index a universe once, avoiding one matrix resize per new symbol."""
        for symbol in symbols:
            if symbol not in self._index:
                self._index[symbol] = len(self._index)
                self._sessions.append(None)
        if len(self._index) > len(self._counts):
            size = ((len(self._index) + 63) // 64) * 64
            old_size = len(self._counts)
            stamps = np.empty((size, self._capacity), dtype=np.float64)
            prices = np.empty_like(stamps)
            stamps[:old_size] = self._timestamps
            prices[:old_size] = self._prices
            starts, counts = np.zeros(size, dtype=np.int64), np.zeros(size, dtype=np.int64)
            starts[:old_size], counts[:old_size] = self._starts, self._counts
            self._timestamps, self._prices = stamps, prices
            self._starts, self._counts = starts, counts

    def _grow(self) -> None:
        capacity = self._capacity + max(8, self._capacity // 2)
        stamps = np.empty((len(self._counts), capacity), dtype=np.float64)
        prices = np.empty_like(stamps)
        for index in range(len(self._index)):
            count, start = int(self._counts[index]), int(self._starts[index])
            first = min(count, self._capacity - start)
            for source, target in ((self._timestamps, stamps), (self._prices, prices)):
                target[index, :first] = source[index, start:start + first]
                target[index, first:count] = source[index, :count - first]
        self._timestamps, self._prices = stamps, prices
        self._starts.fill(0)
        self._capacity = capacity

    def append(self, symbol: str, point: RealtimePoint) -> bool:
        return self.append_price(symbol, point.timestamp, point.last_price)

    def append_price(self, symbol: str, timestamp: float, price: float) -> bool:
        """Store two native doubles; do not construct a RealtimePoint per tick."""
        if not math.isfinite(timestamp) or not math.isfinite(price):
            return False
        session = self.trading_session(timestamp)
        if session is None:
            return False
        if symbol not in self._index:
            self.reserve_symbols((symbol,))
        index = self._index[symbol]
        count, start = int(self._counts[index]), int(self._starts[index])
        if count:
            last = (start + count - 1) % self._capacity
            if timestamp < self._timestamps[index, last]:
                return False
        if self._sessions[index] != session:
            count = start = 0
            self._sessions[index] = session
        if count and timestamp == self._timestamps[index, (start + count - 1) % self._capacity]:
            self._prices[index, (start + count - 1) % self._capacity] = price
            return True
        cutoff = timestamp - self.retention_seconds
        # Keep the latest point <= cutoff, even across a gap longer than the
        # margin. It may be the only legal reference for a 5-minute speed.
        while count > 1 and self._timestamps[index, (start + 1) % self._capacity] <= cutoff:
            start = (start + 1) % self._capacity
            count -= 1
        self._starts[index], self._counts[index] = start, count
        if count == self._capacity:
            self._grow()
            start = 0
        slot = (start + count) % self._capacity
        self._timestamps[index, slot] = timestamp
        self._prices[index, slot] = price
        self._counts[index] = count + 1
        return True

    def _position(self, index: int, offset: int) -> int:
        return (int(self._starts[index]) + offset) % self._capacity

    def points(self, symbol: str) -> list[RealtimePoint]:
        """Single-symbol price chart view; volume/amount remain in current ticks."""
        index = self._index.get(symbol)
        if index is None:
            return []
        return [RealtimePoint(float(self._timestamps[index, self._position(index, offset)]),
                              float(self._prices[index, self._position(index, offset)]))
                for offset in range(int(self._counts[index]))]

    def prices(self, symbol: str, limit: int = 120) -> list[float]:
        index = self._index.get(symbol)
        if index is None:
            return []
        count = int(self._counts[index])
        return [float(self._prices[index, self._position(index, offset)])
                for offset in range(count)[-limit:]]

    def speed(self, symbol: str, minutes: int) -> float | str:
        index = self._index.get(symbol)
        if index is None or not self._counts[index]:
            return UNAVAILABLE
        count = int(self._counts[index])
        last = self._position(index, count - 1)
        target = float(self._timestamps[index, last]) - minutes * 60
        low, high = 0, count
        # Upper bound over the logical ring: latest actual quote <= target.
        while low < high:
            middle = (low + high) // 2
            if self._timestamps[index, self._position(index, middle)] <= target:
                low = middle + 1
            else:
                high = middle
        if low == 0:
            return UNAVAILABLE
        reference = float(self._prices[index, self._position(index, low - 1)])
        if reference <= 0:
            return UNAVAILABLE
        current = float(self._prices[index, last])
        return round((current / reference - 1) * 100, 4)

    def diagnostics(self) -> dict:
        counts = self._counts[:len(self._index)]
        symbols = int(np.count_nonzero(counts))
        points = int(counts.sum())
        return {
            "snapshot_symbols": symbols,
            "snapshot_points": points,
            "snapshot_max_symbol_length": int(counts.max()) if len(counts) else 0,
            "snapshot_avg_symbol_length": points / symbols if symbols else 0.0,
            "snapshot_per_symbol_limit": None,  # Time-bounded, no fixed sample cap.
            "history_retention_seconds": self.retention_seconds,
            "history_slot_capacity": self._capacity,
            "history_storage_bytes": sum(array.nbytes for array in
                                         (self._timestamps, self._prices, self._starts, self._counts)),
            "history_storage": "float64_timestamp_price_matrices",
            "history_retained_realtime_points": 0,
        }


class RealtimeSnapshotBuffer(CompactMarketHistory):
    """Compatibility name for existing feed and UI callers."""
