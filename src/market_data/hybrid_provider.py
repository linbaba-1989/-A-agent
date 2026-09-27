"""Consumer facade: one realtime source per quote, independent official reference/history."""
from dataclasses import asdict
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Event, RLock, Thread
from time import monotonic
from .contracts import canonical_symbol
from .free_provider import FreeMarketDataProvider
from .free_history_provider import FreeHistoryProvider
from .hithink_provider import HithinkOfficialProvider
from .sector_service import SectorService
from ..market_cache import normalize_instrument
from ..models import MarketDiagnostics

HISTORY_FALLBACK_STATUSES = {"AUTH_NOT_CONFIGURED", "AUTH_INVALID", "PERMISSION_DENIED",
                             "RATE_LIMITED", "UPSTREAM_UNAVAILABLE"}
INDEX_CODES = ("000001.SH", "399001.SZ", "399006.SZ")


class HybridMarketDataProvider:
    provider_name = "HybridMarketDataProvider"
    hybrid = True

    def __init__(self, realtime=None, official=None, history_fallback=None, timer=monotonic):
        self.realtime = realtime if realtime is not None else FreeMarketDataProvider()
        self.official = official if official is not None else HithinkOfficialProvider()
        self.history_fallback = history_fallback if history_fallback is not None else FreeHistoryProvider()
        self.sectors = SectorService(self.official.sectors)
        self.timer = timer
        self._securities, self._instrument_cache = {}, {}
        self._universe_at = float("-inf")
        self._reference_lock, self._history_lock = RLock(), RLock()
        self._index_lock = RLock()
        self._history, self._fallback_until = {}, {}
        self.last_batch = None
        self._index, self._index_at = None, float("-inf")
        self._calendar_at = float("-inf")
        for adapter in (getattr(self.realtime, "primary", None), getattr(self.realtime, "fallback", None)):
            if hasattr(adapter, "freshness"):
                adapter.freshness.calendar = self.official.calendar
        self._stop, self._queue = Event(), Queue(maxsize=100)
        self._pending, self._worker = set(), None
        self._sector_worker = None
        self._index_worker = None
        self.sector_sync_status = "NOT_STARTED"
        self._states = dict(realtime="tencent:NOT_LOADED", history="hithink:NOT_LOADED",
                            universe="hithink:NOT_LOADED", index="hithink:NOT_LOADED",
                            calendar="hithink:NOT_LOADED")

    def get_stock_universe(self):
        if self.timer() - self._universe_at < (86400 if self._securities else 300):
            return list(self._securities)
        with self._reference_lock:
            if self.timer() - self._universe_at < (86400 if self._securities else 300):
                return list(self._securities)
            result = self.official.universe.refresh()
            if result.status in {"SUCCESS", "CACHED"} and self.official.universe.rows:
                self._securities = dict(self.official.universe.rows)
                self._states["universe"] = "hithink:" + result.status
            else:
                self._states["universe"] = "free_universe:DEGRADED:" + result.status
                try:
                    rows = self.realtime.universe.get()
                    self._securities = {r.symbol: asdict(r) for r in rows}
                except RuntimeError:
                    self._states["universe"] = "free_universe:UNAVAILABLE:" + result.status
            self._universe_at = self.timer()
            self._instrument_cache = {s: dict(InstrumentName=r.get("name"), reference_source=
                "hithink" if self._states["universe"].startswith("hithink:") else "free_universe")
                for s, r in self._securities.items()}
            return list(self._securities)

    def snapshot(self, symbols=None):
        # No official lock or reference call in explicit-symbol realtime polling.
        symbols = self.get_stock_universe() if symbols is None else symbols
        batch = self.realtime.snapshot(symbols)
        self.last_batch = batch
        self._states["realtime"] = batch.source + ":" + (
            "AVAILABLE" if batch.valid_symbols else "UNAVAILABLE")
        return batch

    def snapshot_all(self):
        return self.snapshot()

    def quote(self, symbol):
        symbol = canonical_symbol(symbol)
        return self.snapshot([symbol]).snapshots[symbol]

    def get_daily_history(self, symbol, count=120, adjustment="raw", network=True):
        with self._history_lock:
            result = self.official.history.load(symbol, count, adjustment, kind="equity", network=network)
            reasons = set(result.warnings) & HISTORY_FALLBACK_STATUSES
            key = (symbol, adjustment)
            if reasons:
                self._fallback_until[key] = (self.timer() + 300, sorted(reasons))
            elif result.quality_status == "VALID" and len(result.bars) >= count:
                self._fallback_until.pop(key, None)
            cooldown = self._fallback_until.get(key)
            fallback_allowed = bool(reasons or (cooldown and self.timer() < cooldown[0]))
            if fallback_allowed:
                alternative = self.history_fallback.daily(symbol, count, adjustment, kind="equity", network=network)
                if alternative.bars:
                    alternative.fallback_used = True
                    alternative.warnings = ["hithink:" + r for r in (sorted(reasons) or cooldown[1])] + alternative.warnings
                    result = alternative
            self._history[symbol] = result
            self._states["history"] = result.source + ":" + result.quality_status
            if fallback_allowed:
                self._states["history"] += ":DEGRADED:hithink=" + ",".join(sorted(reasons) or cooldown[1])
            return result

    def get_history(self, symbols, period="1d", count=120, **kwargs):
        if period != "1d":
            raise ValueError("only_daily_supported")
        return {s: self.get_daily_history(s, count, **kwargs).frame() for s in dict.fromkeys(symbols)}

    def get_local_history(self, symbols, count=120):
        return self.get_history(symbols, count=count, network=False)

    def queue_history(self, symbols, count=65):
        # A scan prioritizes a bounded candidate batch; no all-A synchronous REST download.
        for symbol in list(symbols)[:30]:
            with self._reference_lock:
                if symbol in self._pending:
                    continue
                try:
                    self._queue.put_nowait((symbol, count))
                    self._pending.add(symbol)
                except Full:
                    break
        with self._reference_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = Thread(target=self._warm_history, daemon=True, name="hybrid-history")
                self._worker.start()

    def _warm_history(self):
        while not self._stop.is_set():
            try:
                symbol, count = self._queue.get(timeout=.5)
            except Empty:
                continue
            try:
                self.get_daily_history(symbol, count)
            except Exception as exc:
                self._states["history"] = "DEGRADED:" + type(exc).__name__
            finally:
                with self._reference_lock:
                    self._pending.discard(symbol)
                self._queue.task_done()
            if self._stop.wait(.5):
                break

    def get_index_snapshot(self, symbols=INDEX_CODES):
        with self._index_lock:
            key = tuple(symbols)
            if self._index is not None and self._index[0] == key and self.timer() - self._index_at < 30:
                return self._index[1]
            batch = self.official.index_snapshot(list(symbols))
            self._index, self._index_at = (key, batch), self.timer()
            self._states["index"] = "hithink:" + batch.capability_status
            return batch

    def cached_index_snapshot(self):
        """UI pollers never wait for official HTTP or its cooldown lock."""
        if self.timer() - self._index_at >= 30 and (self._index_worker is None or not self._index_worker.is_alive()):
            def refresh():
                try:
                    self.get_index_snapshot()
                except Exception as exc:
                    self._states["index"] = "hithink:DEGRADED:" + type(exc).__name__
                    self._index_at = self.timer()
            self._index_worker = Thread(target=refresh, daemon=True, name="hybrid-index")
            self._index_worker.start()
        return self._index[1] if self._index else None

    def get_index_history(self, symbol, count=120):
        return self.official.history.load(symbol, count, kind="index")

    def get_calendar(self):
        if self.timer() - self._calendar_at >= 300:
            result = self.official.calendar.refresh()
            self._calendar_at = self.timer()
            self._states["calendar"] = "hithink:" + result.status
        return self.official.calendar

    @property
    def universe_audit(self):
        batch = self.last_batch
        return dict(raw_count=len(self._securities), active_count=len(self._securities),
                    valid_tick_count=len(batch.valid_symbols) if batch else 0)

    def get_sector_catalog(self, tag="industry"):
        return self.sectors.catalog(tag)

    def get_sector_members(self, code):
        return self.sectors.members(code)

    def get_stock_sectors(self, symbol):
        return self.sectors.memberships(symbol)

    def sync_sectors(self):
        if self._sector_worker is None or not self._sector_worker.is_alive():
            def run():
                self.sector_sync_status = "SYNCING"
                try:
                    self.sector_sync_status = self.sectors.sync(self._stop)
                except Exception as exc:
                    self.sector_sync_status = "DEGRADED:" + type(exc).__name__
            self._sector_worker = Thread(target=run, daemon=True, name="hybrid-sectors")
            self._sector_worker.start()

    def provider_status(self):
        return {**self._states, "official_auth": "CONFIGURED" if self.official.client.configured
                else "AUTH_NOT_CONFIGURED", "history_pending": len(self._pending),
                "sector_sync": self.sector_sync_status}

    def get_instrument_detail(self, symbol):
        if not self._securities:
            self.get_stock_universe()
        return dict(self._instrument_cache.get(symbol, {}))

    def normalized_instrument(self, symbol):
        return normalize_instrument(symbol, self.get_instrument_detail(symbol))

    @staticmethod
    def _tick(quote):
        data = quote.to_dict()
        return dict(lastPrice=quote.price, lastClose=quote.prev_close, open=quote.open,
                    high=quote.high, low=quote.low, volume=quote.volume_shares, amount=quote.amount_cny,
                    time=quote.quote_time.timestamp() * 1000 if quote.quote_time else None,
                    source=quote.source, quote_status=quote.quote_status, volume_unit="shares",
                    turnover_rate=quote.turnover_rate, volume_ratio=quote.volume_ratio,
                    total_market_cap=quote.total_market_cap, float_market_cap=quote.float_market_cap,
                    field_provenance={k: quote.source if v is not None else None for k, v in data.items()})

    def get_full_ticks(self, symbols):
        return {s: self._tick(q) for s, q in self.snapshot(symbols).snapshots.items()}

    def get_market_ticks(self):
        return self.get_full_ticks(self.get_stock_universe())

    @staticmethod
    def tick_timestamp(tick):
        return tick.get("time") / 1000 if tick and tick.get("time") is not None else None

    @staticmethod
    def _valid_tick(tick, require_time=True):
        return bool(tick and tick.get("lastPrice") is not None and tick["lastPrice"] > 0
                    and (not require_time or tick.get("time")) and tick.get("quote_status") != "UNAVAILABLE")

    def normalize_tick(self, symbol, tick):
        from ..market_clock import timestamp_to_beijing
        stamp = self.tick_timestamp(tick)
        return {**tick, "symbol": symbol, "timestamp": timestamp_to_beijing(stamp).isoformat() if stamp else None}

    def connection_diagnostics(self):
        # Constructible without network/paid credentials; actual quote quality is reported separately.
        return MarketDiagnostics(connected=True, xtquant_path=None, message="hybrid initialized; quote status separate")

    def provenance(self, symbol, tick, frame):
        history = frame.attrs.get("source", "unavailable") if frame is not None else "unavailable"
        reference = self._instrument_cache.get(symbol, {}).get("reference_source", "unavailable")
        realtime = tick.get("source", "unavailable")
        fields = dict(tick.get("field_provenance", {}))
        fields.update({k: realtime if tick.get(k) is not None else None for k in
                       ("turnover_rate", "total_market_cap", "float_market_cap")})
        fields.update({k: realtime for k in ("last_price", "previous_close", "change_pct", "speed_1m", "speed_3m", "speed_5m")})
        fields.update({k: history for k in ("recent_daily_k", "atr14", "high_5d", "high_10d", "high_20d")})
        fields.update({k: [realtime, history] for k in ("ma5", "ma10", "ma20", "ma60", "volume_ratio")})
        fields["name"] = reference
        return dict(source="hybrid", realtime_source=realtime, history_source=history,
                    reference_source=reference, field_provenance=fields, quote_status=tick.get("quote_status"),
                    history_adjustment=frame.attrs.get("adjustment") if frame is not None else None,
                    history_quality=frame.attrs.get("quality_status") if frame is not None else "UNAVAILABLE")

    def close(self):
        self._stop.set()
        self.realtime.close()
        self.official.close()
