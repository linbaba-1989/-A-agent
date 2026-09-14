"""Read-only snapshot polling and in-memory UI state for live market views."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time
from threading import Lock
from time import perf_counter
from typing import Any, Iterable

from .indicators import UNAVAILABLE, percent_change
from .market_cache import normalize_instrument, validated_turnover


@dataclass(frozen=True)
class RealtimePoint:
    timestamp: float
    last_price: float
    volume: float | None = None
    amount: float | None = None
    turnover: float | None = None


class RealtimeSnapshotBuffer:
    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self._points: dict[str, deque[RealtimePoint]] = defaultdict(lambda: deque(maxlen=maxlen))
        self._sessions: dict[str, tuple[str, str]] = {}

    @staticmethod
    def trading_session(timestamp: float) -> tuple[str, str] | None:
        moment = datetime.fromtimestamp(timestamp)
        clock = moment.time()
        if time(9, 30) <= clock <= time(11, 30):
            return moment.date().isoformat(), "morning"
        if time(13, 0) <= clock <= time(15, 0):
            return moment.date().isoformat(), "afternoon"
        return None

    def append(self, symbol: str, point: RealtimePoint) -> bool:
        session = self.trading_session(point.timestamp)
        if session is None:
            return False
        series = self._points[symbol]
        if self._sessions.get(symbol) != session:
            series.clear()
            self._sessions[symbol] = session
        if series and point.timestamp < series[-1].timestamp:
            return False
        if series and point.timestamp == series[-1].timestamp:
            series[-1] = point
        else:
            series.append(point)
        return True

    def points(self, symbol: str) -> list[RealtimePoint]:
        return list(self._points.get(symbol, ()))

    def prices(self, symbol: str, limit: int = 120) -> list[float]:
        return [point.last_price for point in self.points(symbol)[-limit:]]

    def speed(self, symbol: str, minutes: int) -> float | str:
        series = self._points.get(symbol)
        if not series:
            return UNAVAILABLE
        current = series[-1]
        target = current.timestamp - minutes * 60
        eligible = [point for point in series if point.timestamp <= target]
        if not eligible:
            return UNAVAILABLE
        reference = max(eligible, key=lambda point: point.timestamp)
        if reference.last_price <= 0:
            return UNAVAILABLE
        return round((current.last_price / reference.last_price - 1) * 100, 4)


class SnapshotConsumerState:
    """Per-view event state; flash events never enter the shared market cache."""
    def __init__(self):
        self.last_seen_snapshot_seq: dict[str, int] = {}
        self.last_seen_price: dict[str, float] = {}

    def consume(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        viewed = []
        for source in rows:
            row = dict(source)
            symbol = str(row["symbol"])
            seq = int(row.get("snapshot_seq", 0))
            previous_seq = self.last_seen_snapshot_seq.get(symbol)
            previous_price = self.last_seen_price.get(symbol)
            flash = ""
            if previous_seq is not None and seq > previous_seq:
                flash = price_flash(previous_price, row.get("lastPrice"))
            if previous_seq is None or seq > previous_seq:
                self.last_seen_snapshot_seq[symbol] = seq
                try:
                    self.last_seen_price[symbol] = float(row["lastPrice"])
                except (KeyError, TypeError, ValueError):
                    pass
            row["flash_class"] = flash
            viewed.append(row)
        return viewed


def price_flash(previous: Any, current: Any) -> str:
    try:
        before, after = float(previous), float(current)
    except (TypeError, ValueError):
        return ""
    return "price-flash-up" if after > before else "price-flash-down" if after < before else ""


def live_state(previous_timestamp: float | None, current_timestamp: float | None) -> str:
    if current_timestamp is None:
        return "STALE"
    if RealtimeSnapshotBuffer.trading_session(current_timestamp) is None:
        return "CLOSED"
    return "LIVE" if previous_timestamp is not None and current_timestamp > previous_timestamp else "STALE"


def refresh_interval_seconds(market_open: bool, enabled: bool, requested: int) -> int | None:
    if not market_open or not enabled:
        return None
    return requested if requested in (1, 2, 5) else 2


def rank_rows(rows: Iterable[dict[str, Any]], metric: str, top_n: int = 20) -> list[dict[str, Any]]:
    reverse = metric != "change_pct_asc"
    field = "change_pct" if metric == "change_pct_asc" else metric
    valid = []
    for row in rows:
        try:
            value = float(row.get(field))
        except (TypeError, ValueError):
            continue
        valid.append((value, row))
    valid.sort(key=lambda item: item[0], reverse=reverse)
    return [row for _, row in valid[:max(0, top_n)]]


class RealtimeMarketFeed:
    """Synchronous feed. A non-blocking lock prevents overlapping provider calls."""
    def __init__(self, provider: Any, scanner: Any = None, maxlen: int = 300):
        self.provider = provider
        self.scanner = scanner
        self.buffer = RealtimeSnapshotBuffer(maxlen)
        self._lock = Lock()
        self._details: dict[str, dict[str, Any]] = {}
        self._latest_ticks: dict[str, dict[str, Any]] = {}
        self.last_quote_timestamp: float | None = None
        self.snapshot_latency = 0.0
        self.provider_initializations = 1
        self.provider_request_count = 0
        self.skipped_due_to_lock = 0
        self.overlapping_request_count = 0
        self.cache_hit_count = 0
        self._latest_rows: dict[str, dict[str, Any]] = {}
        self._last_request_finished = 0.0
        self._minimum_request_gap = 0.5
        self.snapshot_seq = 0

    def universe(self) -> list[str]:
        return self.provider.get_stock_universe()

    def snapshot(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            self.skipped_due_to_lock += 1
            return self.cached(symbols)
        started = perf_counter()
        try:
            selected = symbols if symbols is not None else self.universe()
            if (self._latest_rows and perf_counter() - self._last_request_finished < self._minimum_request_gap
                    and all(symbol in self._latest_rows for symbol in selected)):
                self.cache_hit_count += 1
                self.snapshot_latency = 0.0
                return self.cached(selected)
            self.provider_request_count += 1
            ticks = self.provider.get_full_ticks(selected)
            self.snapshot_seq += 1
            source_fetch_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
            rows = []
            latest = None
            for symbol in selected:
                tick = ticks.get(symbol)
                if not tick or not self.provider._valid_tick(tick):
                    continue
                timestamp = self.provider.tick_timestamp(tick)
                if timestamp is None:
                    continue
                latest = max(latest or timestamp, timestamp)
                try:
                    last = float(tick["lastPrice"])
                except (KeyError, TypeError, ValueError):
                    continue
                detail = (getattr(self.scanner, "instrument_cache", {}) or {}).get(symbol, {})
                raw_detail = (getattr(self.provider, "_instrument_cache", {}) or {}).get(symbol)
                if not detail and raw_detail:
                    detail = normalize_instrument(symbol, raw_detail)
                detail = detail or self._details.get(symbol, {})
                turnover = validated_turnover(tick, detail).get("value", UNAVAILABLE) if detail else UNAVAILABLE
                point = RealtimePoint(timestamp, last, _float(tick.get("volume")), _float(tick.get("amount")),
                                      _float(turnover))
                self.buffer.append(symbol, point)
                self._latest_ticks[symbol] = tick
                rows.append({"symbol": symbol, "name": detail.get("name", ""), "lastPrice": last,
                             "lastClose": tick.get("lastClose", UNAVAILABLE),
                             "change_pct": percent_change(last, tick.get("lastClose")),
                             "speed_1m": self.buffer.speed(symbol, 1),
                             "speed_3m": self.buffer.speed(symbol, 3),
                             "speed_5m": self.buffer.speed(symbol, 5), "turnover_rate": turnover,
                             "amount": tick.get("amount", UNAVAILABLE), "volume": tick.get("volume", UNAVAILABLE),
                             "high": tick.get("high", UNAVAILABLE), "low": tick.get("low", UNAVAILABLE),
                             "open": tick.get("open", UNAVAILABLE),
                             "quote_time": datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
                             "quote_timestamp": timestamp, "snapshot_seq": self.snapshot_seq,
                             "source_fetch_time": source_fetch_time, "from_cache": False})
            self._latest_rows.update({row["symbol"]: row for row in rows})
            self.last_quote_timestamp = latest
            return rows
        finally:
            self.snapshot_latency = perf_counter() - started
            self._last_request_finished = perf_counter()
            self._lock.release()

    def cached(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        if symbols is None:
            rows = [dict(row) for row in self._latest_rows.values()]
        else:
            rows = [dict(self._latest_rows[symbol]) for symbol in symbols if symbol in self._latest_rows]
        for row in rows:
            row["from_cache"] = True
        return rows

    def enrich_static(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Load contract names/capital once for the small set currently visible."""
        for row in rows:
            symbol = row["symbol"]
            if symbol not in self._details:
                try:
                    self._details[symbol] = self.provider.normalized_instrument(symbol)
                except Exception:
                    self._details[symbol] = {}
            detail = self._details[symbol]
            row["name"] = detail.get("name") or row.get("name", "")
            tick = self._latest_ticks.get(symbol)
            if tick and detail:
                row["turnover_rate"] = validated_turnover(tick, detail).get("value", UNAVAILABLE)
        return rows


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
