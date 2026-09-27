"""Opt-in completed daily service; preserves DataFrame call shape, not QMT lot units."""
from datetime import date, timedelta
from threading import RLock
from time import monotonic, perf_counter
from .contracts import canonical_symbol
from .historical import ADJUSTMENTS, INDEX_SYMBOLS, HistoryResult, completed_date, validate
from .history_cache import FreeHistoryCache
from .adapters.history import AKShareHistoryProvider, BaoStockHistoryProvider
from ..market_clock import to_beijing


class FreeHistoryProvider:
    def __init__(self, primary=None, fallback=None, cache=None, clock=to_beijing,
                 timer=monotonic, retry_seconds=300):
        self.primary = primary if primary is not None else AKShareHistoryProvider()
        self.fallback = fallback if fallback is not None else BaoStockHistoryProvider()
        self.cache = cache if cache is not None else FreeHistoryCache()
        self.clock, self.timer, self.retry_seconds = clock, timer, retry_seconds
        self._retry = {}
        self._lock = RLock()
        self.last_results = {}

    def _obtain(self, provider, symbol, start, end, count, adjustment, kind, now, network):
        cached = self.cache.read(symbol, provider.name, adjustment, kind, provider.endpoint, now)
        enough = cached and len([b for b in cached.bars if b.trade_date <= end]) >= count
        # Count-based API: an adequate tail ending at the target is a cache hit.
        if enough and any(b.trade_date == end for b in cached.bars):
            return cached
        key = (provider.name, provider.endpoint, symbol, adjustment, kind)
        if not network or self.timer() < self._retry.get(key, float("-inf")):
            result = cached or HistoryResult(symbol, source=provider.name, adjustment=adjustment)
            if cached:
                result.data_status, result.quality_status = "CACHED", "DEGRADED"
                result.warnings.append("cache_not_current_or_insufficient")
            return result
        request_start = cached.bars[-1].trade_date if cached and enough else start
        fetched = provider.daily(symbol, request_start, end, adjustment, kind)
        fetched.updated_at = now.isoformat()
        if fetched.quality_status == "VALID" and fetched.bars:
            if cached and request_start != start:
                old = {b.trade_date: b for b in cached.bars}
                overlap = [b for b in fetched.bars if b.trade_date in old]
                changed_basis = adjustment != "raw" and (not overlap or any(
                    any(getattr(b, k) != getattr(old[b.trade_date], k) for k in ("open", "high", "low", "close"))
                    for b in overlap))
                if changed_basis:
                    # Corporate-action rebase: replace entire retained window; never append mismatched bases.
                    fetched = provider.daily(symbol, cached.bars[0].trade_date, end, adjustment, kind)
                    fetched.warnings.append("adjusted_basis_changed_full_refresh")
                else:
                    merged = {b.trade_date: b for b in cached.bars}
                    merged.update({b.trade_date: b for b in fetched.bars})
                    combined = validate(list(merged.values()), symbol, provider.name, adjustment,
                                        min(start, cached.bars[0].trade_date), end, now)
                    combined.warnings.extend(fetched.warnings)
                    fetched = combined
            self.cache.write(fetched, kind, provider.endpoint, now)
        if (fetched.quality_status != "VALID" or len(fetched.bars) < count
                or not fetched.bars or fetched.bars[-1].trade_date < end):
            self._retry[key] = self.timer() + self.retry_seconds
        if not fetched.bars and cached:
            cached.quality_status = "DEGRADED"
            cached.warnings.extend(fetched.warnings + ["cached_after_fetch_failure"])
            return cached
        return fetched

    def daily(self, symbol, count=120, adjustment="raw", kind=None, end=None, network=True):
        symbol = canonical_symbol(symbol)
        if adjustment not in ADJUSTMENTS or not isinstance(count, int) or not 1 <= count <= 5000:
            raise ValueError("invalid_history_request")
        kind = kind or ("index" if symbol in INDEX_SYMBOLS else "equity")
        if kind not in {"equity", "index"}:
            raise ValueError("invalid_kind")
        now = self.clock()
        if end is not None and (not isinstance(end, date) or hasattr(end, "hour")):
            raise ValueError("end_must_be_trade_date")
        target = min(end or completed_date(now), completed_date(now))
        # Holidays and suspended days need slack; never synthesize the requested count.
        start = target - timedelta(days=max(30, int(count * 2.2) + 14))
        started = perf_counter()
        with self._lock:
            primary = self._obtain(self.primary, symbol, start, target, count, adjustment, kind, now, network)
            enough = lambda r: (r.quality_status == "VALID" and len(r.bars) >= count
                                 and r.bars[-1].trade_date >= target)
            result = primary
            if not enough(primary) and self.fallback.supports(symbol, kind):
                alternative = self._obtain(self.fallback, symbol, start, target, count,
                                           adjustment, kind, now, network)
                if enough(alternative) or (alternative.quality_status == "VALID" and
                        primary.quality_status != "VALID" and alternative.bars):
                    result = alternative
                    result.fallback_used = True
                    result.warnings = ["primary:" + w for w in primary.warnings] + result.warnings
            if symbol.endswith(".BJ") and not enough(result):
                result.warnings.append("BSE_HISTORY_DEGRADED")
            result.bars = [b for b in result.bars if b.trade_date <= target][-count:]
            result.requested_end = target
            if len(result.bars) < count:
                result.warnings.append("insufficient_history:" + str(count))
                if result.bars and result.data_status != "CACHED":
                    result.data_status = "PARTIAL"
            if result.bars and result.bars[-1].trade_date < target:
                result.quality_status = "DEGRADED"
                if result.data_status != "CACHED":
                    result.data_status = "PARTIAL"
            result.latency = perf_counter() - started
            self.last_results[symbol] = result
            return result

    def batch(self, symbols, count=120, adjustment="raw", **kwargs):
        return {s: self.daily(s, count, adjustment, **kwargs) for s in dict.fromkeys(symbols)}

    def get_history(self, symbols, period="1d", count=120, **kwargs):
        if period != "1d":
            raise ValueError("only_daily_supported")
        return {s: r.frame() for s, r in self.batch(symbols, count, **kwargs).items()}

    def get_local_history(self, symbols, count=120, **kwargs):
        return self.get_history(symbols, "1d", count, network=False, **kwargs)


def create_free_history_provider(environ=None, **kwargs):
    from .factory import market_data_mode
    if market_data_mode(environ) != "free":
        raise ValueError("free_history_requires_explicit_free_mode")
    return FreeHistoryProvider(**kwargs)
