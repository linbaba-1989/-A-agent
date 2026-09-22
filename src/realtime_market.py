"""Read-only snapshot polling and in-memory UI state for live market views."""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timedelta
import statistics
import math
from threading import Lock
from time import perf_counter
from typing import Any, Iterable

from .compact_market_history import CompactMarketHistory, RealtimePoint, RealtimeSnapshotBuffer
from .indicators import UNAVAILABLE, percent_change
from .market_cache import normalize_instrument, validated_turnover
from .last_valid_snapshot import DEFAULT_PATH, TOKEN_SOURCE, LastValidSnapshot
from .market_clock import (OPEN, QuoteEvidence, should_fetch_quotes, timestamp_to_beijing, to_beijing,
                           quote_status as market_quote_status)


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


def market_quote_timestamp(rows: Iterable[dict[str, Any]], fallback: float | None = None) -> float | None:
    """Return the newest valid market timestamp, including the feed watermark.

    A watchlist or research view may contain one symbol whose last trade is
    older than the rest of the market.  The feed watermark therefore remains
    a candidate even when the supplied rows are a subset.
    """

    timestamps = []
    for row in rows:
        value = row.get("quote_timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            timestamps.append(float(value))
    if fallback is not None and isinstance(fallback, (int, float)) and not isinstance(fallback, bool) and fallback > 0:
        timestamps.append(float(fallback))
    return max(timestamps, default=None)


def _quote_time_text(value: float | None) -> str | None:
    moment = timestamp_to_beijing(value)
    return moment.isoformat(timespec="milliseconds") if moment else None


def _raw_timestamp_field(tick: dict[str, Any]) -> str:
    if tick.get("time"):
        return "time"
    if tick.get("timetag"):
        return "timetag"
    return "none"


def _row_signature(row: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    ignored = {"snapshot_seq", "source_fetch_time", "from_cache", "flash_class"}
    return tuple(sorted((key, repr(value)) for key, value in row.items() if key not in ignored))


def quote_status(previous_timestamp: float | None, current_timestamp: float | None,
                 market_session: str = OPEN, now: datetime | None = None) -> str:
    """Return quote freshness; market session is never inferred from quote time."""

    return market_quote_status(previous_timestamp, current_timestamp, market_session, now)


def live_state(previous_timestamp: float | None, current_timestamp: float | None,
               market_session: str | None = None, now: datetime | None = None) -> str:
    """Backward-compatible live-state helper.

    New UI code uses :func:`quote_status`, which has no ``CLOSED`` quote
    state.  The two-argument form is retained for older acceptance scripts.
    """

    if market_session is not None:
        return quote_status(previous_timestamp, current_timestamp, market_session, now)
    if current_timestamp is None:
        return "STALE"
    if RealtimeSnapshotBuffer.trading_session(current_timestamp) is None:
        return "CLOSED"
    return "LIVE" if previous_timestamp is not None and current_timestamp > previous_timestamp else "STALE"


def refresh_interval_seconds(market_session: str | bool, enabled: bool, requested: int) -> int | None:
    """Return a high-frequency interval only for continuous trading."""

    is_open = market_session is True or market_session == OPEN
    if not is_open or not enabled:
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
    def __init__(self, provider: Any, scanner: Any = None, maxlen: int = 300, *, snapshot_cache=DEFAULT_PATH):
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
        self.quote_evidence = QuoteEvidence()
        self._last_probe_key: tuple[str, str] | None = None
        self._diagnostic_request_id = 0
        self.request_diagnostics: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_request_diagnostic: dict[str, Any] | None = None
        self._token_source = getattr(provider, "provider_name", None) == TOKEN_SOURCE
        self._snapshot_store = (LastValidSnapshot(snapshot_cache) if snapshot_cache is not None
                                and (self._token_source or provider is None) else None)
        self._closed_probe_lock = Lock()
        self._closed_probe_key = None
        self.snapshot_cache_error = False
        self._has_full_market_snapshot = False
        if self._snapshot_store is not None:
            restored = self._snapshot_store.load()
            self._has_full_market_snapshot = bool(restored)
            self._latest_rows = {row["symbol"]: row for row in restored}
            self.snapshot_seq = max((row["snapshot_seq"] for row in restored), default=0)
            self.last_quote_timestamp = market_quote_timestamp(restored)
            # Restoring disk data is not fresh-fetch evidence and cannot establish LIVE.
            self.quote_evidence.quote_timestamp = self.last_quote_timestamp
            self.quote_evidence.valid_quote_count = len(restored)
        if provider is None:
            self.provider_initializations = 0

    def ensure_closed_snapshot(self, session, now=None):
        """At most one full-market attempt per non-trading session per process.

        Reservation precedes I/O, including failures and concurrent viewers.
        A restored snapshot is displayed by the SSE adapter before this call.
        """
        if should_fetch_quotes(session) or not self._token_source:
            return self.cached()
        key = (to_beijing(now).date(), session)
        with self._closed_probe_lock:
            if key == self._closed_probe_key:
                return self.cached()
            self._closed_probe_key = key
        try:
            self.snapshot(force=True, market_session=session, now=now)
        except Exception:
            pass  # Retain last valid data; never retry every SSE polling interval.
        return self.cached()

    def universe(self) -> list[str]:
        return self.provider.get_stock_universe()

    def _next_diagnostic_request_id(self) -> int:
        self._diagnostic_request_id += 1
        return self._diagnostic_request_id

    def _worker_request_ids(self, before: Any, after: Any) -> list[int]:
        if isinstance(before, int) and isinstance(after, int) and after >= before:
            return list(range(before + 1, after + 1))
        return []

    def _provider_valid_tick(self, tick: dict[str, Any] | None, require_time: bool = True) -> bool:
        try:
            return bool(self.provider._valid_tick(tick, require_time=require_time))
        except TypeError:
            return bool(self.provider._valid_tick(tick))

    def _record_snapshot_diagnostic(self, *, request_id: int | None, worker_request_ids: list[int],
                                    request_started_at: str | None, request_finished_at: str | None,
                                    ticks: dict[str, dict[str, Any]] | None, rows: list[dict[str, Any]],
                                    previous_feed_timestamp: float | None, market_session: str,
                                    now: datetime | None, from_cache: bool, error: BaseException | None = None,
                                    rows_changed_count: int = 0, reason: str | None = None) -> dict[str, Any]:
        raw_timestamps = []
        raw_timestamp_fields = Counter()
        provider_valid_count = 0
        for tick in (ticks or {}).values():
            if not isinstance(tick, dict):
                continue
            raw_timestamp_fields[_raw_timestamp_field(tick)] += 1
            timestamp = self.provider.tick_timestamp(tick)
            if timestamp is not None:
                raw_timestamps.append(float(timestamp))
            if self._provider_valid_tick(tick, require_time=True):
                provider_valid_count += 1
        counts = Counter(raw_timestamps)
        mode_timestamp, mode_count = counts.most_common(1)[0] if counts else (None, 0)
        feed_timestamp = market_quote_timestamp(rows, self.last_quote_timestamp)
        record = {
            "request_id": request_id,
            "worker_request_ids": worker_request_ids,
            "worker_command": "get_full_tick" if request_id is not None else None,
            "request_started_at": request_started_at,
            "request_finished_at": request_finished_at,
            "provider_raw_max_quote_time": _quote_time_text(max(raw_timestamps) if raw_timestamps else None),
            "provider_raw_min_quote_time": _quote_time_text(min(raw_timestamps) if raw_timestamps else None),
            "provider_raw_median_quote_time": _quote_time_text(statistics.median(raw_timestamps) if raw_timestamps else None),
            "provider_raw_mode_quote_time": _quote_time_text(mode_timestamp),
            "provider_raw_mode_count": mode_count,
            "provider_raw_distinct_quote_times": len(counts),
            "provider_raw_timestamp_fields": dict(raw_timestamp_fields),
            "provider_raw_valid_quote_count": provider_valid_count,
            "valid_quote_count": len(rows),
            "feed_snapshot_seq": self.snapshot_seq,
            "feed_quote_timestamp": feed_timestamp,
            "feed_quote_time": _quote_time_text(feed_timestamp),
            "rows_replaced_count": len(rows) if not from_cache else 0,
            "rows_changed_count": rows_changed_count,
            "from_cache": from_cache,
            "quote_status": self.quote_evidence.status(market_session or OPEN, now),
            "fresh_fetch_count": self.quote_evidence.fresh_fetch_count,
            "consecutive_non_advance_count": self.quote_evidence.consecutive_non_advance_count,
            "last_advanced_at": str(self.quote_evidence.last_advanced_at),
            "stale_stop_eligible": self.quote_evidence.stop_eligible(market_session or OPEN, now),
            "error": type(error).__name__ if error else None,
            "reason": reason,
        }
        self.request_diagnostics.append(record)
        self.last_request_diagnostic = record
        return record

    def diagnostics(self) -> list[dict[str, Any]]:
        """Return recent provider/cache evidence without credentials or raw payloads."""

        return [dict(item) for item in self.request_diagnostics]

    def snapshot(self, symbols: list[str] | None = None, *, force: bool = False,
                 market_session: str = OPEN, now: datetime | None = None) -> list[dict[str, Any]]:
        if not self._lock.acquire(blocking=False):
            self.skipped_due_to_lock += 1
            rows = self.cached(symbols)
            self._record_snapshot_diagnostic(request_id=None, worker_request_ids=[],
                                              request_started_at=None, request_finished_at=None,
                                              ticks=None, rows=rows, previous_feed_timestamp=self.last_quote_timestamp,
                                              market_session=market_session, now=now, from_cache=True,
                                              reason="lock_skip")
            return rows
        started = perf_counter()
        request_id = None
        request_started_at = None
        worker_before = None
        previous_feed_timestamp = self.last_quote_timestamp
        previous_rows: dict[str, dict[str, Any]] = {}
        ticks: dict[str, dict[str, Any]] = {}
        diagnostic_recorded = False
        try:
            selected = symbols if symbols is not None else self.universe()
            if (not force and self._latest_rows
                    and perf_counter() - self._last_request_finished < self._minimum_request_gap
                    and all(symbol in self._latest_rows for symbol in selected)):
                self.cache_hit_count += 1
                self.snapshot_latency = 0.0
                rows = self.cached(selected)
                self._record_snapshot_diagnostic(
                    request_id=None, worker_request_ids=[], request_started_at=None, request_finished_at=None,
                    ticks=None, rows=rows, previous_feed_timestamp=previous_feed_timestamp,
                    market_session=market_session, now=now, from_cache=True, reason="minimum_gap")
                return rows
            previous_rows = {symbol: dict(self._latest_rows[symbol]) for symbol in selected if symbol in self._latest_rows}
            self.provider_request_count += 1
            request_id = self._next_diagnostic_request_id()
            request_started_at = to_beijing().isoformat(timespec="milliseconds")
            worker_before = getattr(getattr(self.provider, "backend", None), "_counter", None)
            ticks = self.provider.get_full_ticks(selected)
            request_finished_at = to_beijing().isoformat(timespec="milliseconds")
            worker_after = getattr(getattr(self.provider, "backend", None), "_counter", None)
            self.snapshot_seq += 1
            source_fetch_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
            rows = []
            self.buffer.reserve_symbols(selected)
            latest = None
            for symbol in selected:
                tick = ticks.get(symbol)
                if not tick or not self.provider._valid_tick(tick):
                    continue
                timestamp = self.provider.tick_timestamp(tick)
                if timestamp is None:
                    continue
                try:
                    last = float(tick["lastPrice"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(last) or last <= 0 or not math.isfinite(timestamp):
                    continue
                latest = max(latest or timestamp, timestamp)
                detail = (getattr(self.scanner, "instrument_cache", {}) or {}).get(symbol, {})
                raw_detail = (getattr(self.provider, "_instrument_cache", {}) or {}).get(symbol)
                if not detail and raw_detail:
                    detail = normalize_instrument(symbol, raw_detail)
                detail = detail or self._details.get(symbol, {})
                turnover = validated_turnover(tick, detail).get("value", UNAVAILABLE) if detail else UNAVAILABLE
                self.buffer.append_price(symbol, timestamp, last)
                self._latest_ticks[symbol] = tick
                quote_moment = timestamp_to_beijing(timestamp)
                rows.append({"symbol": symbol, "name": detail.get("name", ""), "lastPrice": last,
                             "lastClose": tick.get("lastClose", UNAVAILABLE),
                             "change_pct": percent_change(last, tick.get("lastClose")),
                             "speed_1m": self.buffer.speed(symbol, 1),
                             "speed_3m": self.buffer.speed(symbol, 3),
                             "speed_5m": self.buffer.speed(symbol, 5), "turnover_rate": turnover,
                             "amount": tick.get("amount", UNAVAILABLE), "volume": tick.get("volume", UNAVAILABLE),
                             "high": tick.get("high", UNAVAILABLE), "low": tick.get("low", UNAVAILABLE),
                             "open": tick.get("open", UNAVAILABLE),
                             "quote_time": quote_moment.replace(tzinfo=None).isoformat(timespec="seconds"),
                             "quote_timestamp": timestamp, "snapshot_seq": self.snapshot_seq,
                             "source": getattr(self.provider, "provider_name", None),
                             "source_fetch_time": source_fetch_time, "from_cache": False})
                cached_row = previous_rows.get(symbol, {})
                if (not should_fetch_quotes(market_session)
                        and cached_row.get("quote_timestamp") == timestamp
                        and cached_row.get("lastPrice") == last):
                    # Same closing quote after restart: retain proven window values.
                    # A newer quote without sufficient history must remain unavailable.
                    for field in ("speed_1m", "speed_3m", "speed_5m"):
                        value = _float(cached_row.get(field))
                        if rows[-1][field] == UNAVAILABLE and value is not None and math.isfinite(value):
                            rows[-1][field] = value
            self._latest_rows.update({row["symbol"]: row for row in rows
                                     if row["quote_timestamp"] >= self._latest_rows.get(
                                         row["symbol"], {}).get("quote_timestamp", 0)})
            if latest is not None:
                self.last_quote_timestamp = max(self.last_quote_timestamp or latest, latest)
            rows_changed_count = sum(
                _row_signature(row) != _row_signature(previous_rows.get(row["symbol"], {}))
                for row in rows
            )
            # Only a successful full-market request establishes market freshness.
            # Symbol-only polling/cache/reruns must not manufacture market stagnation.
            evaluated_at = (to_beijing(now) + timedelta(seconds=perf_counter() - started)
                            if now is not None else to_beijing())
            if symbols is None:
                self.quote_evidence.observe(latest, len(rows), self.snapshot_seq,
                                            market_session, evaluated_at)
                if rows and self._snapshot_store is not None:
                    self._has_full_market_snapshot = True
                    self.snapshot_cache_error = not self._snapshot_store.save(list(self._latest_rows.values()))
            self._record_snapshot_diagnostic(
                request_id=request_id, worker_request_ids=self._worker_request_ids(worker_before, worker_after),
                request_started_at=request_started_at, request_finished_at=request_finished_at,
                ticks=ticks, rows=rows, previous_feed_timestamp=previous_feed_timestamp,
                market_session=market_session, now=evaluated_at, from_cache=False,
                rows_changed_count=rows_changed_count)
            diagnostic_recorded = True
            return rows
        except Exception as exc:
            if request_id is not None and not diagnostic_recorded:
                request_finished_at = to_beijing().isoformat(timespec="milliseconds")
                worker_after = getattr(getattr(self.provider, "backend", None), "_counter", None)
                self._record_snapshot_diagnostic(
                    request_id=request_id, worker_request_ids=self._worker_request_ids(worker_before, worker_after),
                    request_started_at=request_started_at, request_finished_at=request_finished_at,
                    ticks=ticks, rows=[], previous_feed_timestamp=previous_feed_timestamp,
                    market_session=market_session, now=now, from_cache=False, error=exc)
            raise
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

    def _probe_symbols(self, limit: int = 5) -> list[str]:
        if self._latest_rows:
            return list(self._latest_rows)[:limit]
        return self.universe()[:limit]

    def ensure_fresh_provider_probe(self, market_session: str, now: datetime | None = None,
                                     symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch once when entering an auction/open session.

        This deliberately ignores any cached quote timestamp.  The probe is
        keyed only by the Beijing trading date, exchange session, and
        morning/afternoon slot, so a process that stayed alive overnight or
        through lunch probes again.
        """

        if not should_fetch_quotes(market_session):
            return self.cached(symbols)
        current = to_beijing(now)
        # ``open`` occurs twice on a trading day.  Include the half-day slot
        # so 13:00 is a fresh entry into an open session after lunch.
        slot = "morning" if current.hour < 12 else "afternoon"
        key = (current.date().isoformat(), f"{market_session}:{slot}")
        if self._last_probe_key == key:
            return self.cached(symbols)
        # Default startup probe must establish market evidence, not five symbols.
        selected = symbols
        try:
            rows = self.snapshot(selected, force=True, market_session=market_session, now=current)
        finally:
            # A completed provider attempt is enough to avoid repeating the
            # startup probe on every Streamlit rerun; the regular open-session
            # poller remains responsible for subsequent retries/progress.
            self._last_probe_key = key
        return rows

    def fresh_provider_probe(self, market_session: str, now: datetime | None = None,
                             symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Alias with the terminology used by the startup acceptance path."""

        return self.ensure_fresh_provider_probe(market_session, now, symbols)

    def enrich_static(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Load contract names/capital once for the small set currently visible."""
        changed = False
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
            cached = self._latest_rows.get(symbol)
            if cached is not None:
                for field in ("name", "turnover_rate"):
                    if cached.get(field) != row.get(field):
                        cached[field] = row.get(field)
                        changed = True
        if changed and self._snapshot_store is not None and self._has_full_market_snapshot:
            self.snapshot_cache_error = not self._snapshot_store.save(list(self._latest_rows.values()))
        return rows


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
