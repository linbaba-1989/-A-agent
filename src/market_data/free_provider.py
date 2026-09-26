"""Stable facade; consumers never choose or parse a vendor themselves."""
from threading import RLock
from time import monotonic
from .adapters.realtime import TencentRealtimeProvider, SinaRealtimeProvider
from .contracts import canonical_symbol
from .universe import SecurityUniverse


class FreeMarketDataProvider:
    provider_name = "FreeMarketDataProvider"

    def __init__(self, universe=None, primary=None, fallback=None, timer=monotonic,
                 probe_interval=30, switch_cooldown=30):
        self.universe = universe if universe is not None else SecurityUniverse()
        self.primary = primary if primary is not None else TencentRealtimeProvider(self.universe)
        self.fallback = fallback if fallback is not None else SinaRealtimeProvider(self.universe)
        self.active_source = "tencent"
        self.timer, self.probe_interval, self.switch_cooldown = timer, probe_interval, switch_cooldown
        self.last_switch, self.last_probe = float("-inf"), float("-inf")
        self._lock = RLock()

    @property
    def health(self):
        return {"tencent": self.primary.health, "sina": self.fallback.health}

    def get_stock_universe(self):
        return [s.symbol for s in self.universe.get()]

    def quote(self, symbol):
        symbol = canonical_symbol(symbol)
        return self.snapshot([symbol]).snapshots[symbol]

    def snapshot(self, symbols=None):
        with self._lock:
            symbols = self.get_stock_universe() if symbols is None else symbols
            now = self.timer()
            if self.active_source == "tencent":
                batch = self.primary.snapshot(symbols)
                if (self.primary.health.consecutive_failures >= self.primary.health.unavailable_after
                        and now - self.last_switch >= self.switch_cooldown):
                    candidate = self.fallback.snapshot(symbols)
                    if self.fallback.health.last_sample_good:
                        self.active_source, self.last_switch, self.last_probe = "sina", now, now
                        return candidate
                return batch
            if now - self.last_probe >= self.probe_interval:
                self.last_probe = now
                candidate = self.primary.snapshot(symbols)
                if (self.primary.health.state == "HEALTHY"
                        and now - self.last_switch >= self.switch_cooldown):
                    self.active_source, self.last_switch = "tencent", now
                    return candidate
            return self.fallback.snapshot(symbols)

    def close(self):
        pass  # No worker process, token runtime, or persistent socket.
