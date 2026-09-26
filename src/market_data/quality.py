"""Per-source hysteresis and per-symbol, source-time-based freshness."""
from dataclasses import dataclass
from datetime import timedelta
from ..market_clock import DEFAULT_TRADING_CALENDAR, market_session, to_beijing


@dataclass
class Health:
    state: str = "DEGRADED"
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    coverage_ratio: float = 0
    valid_price_ratio: float = 0
    latency: float = 0
    timestamp_advanced: bool | None = None
    reason: str = "warming_up"
    last_sample_good: bool = False
    degrade_after: int = 2
    unavailable_after: int = 3
    recover_after: int = 3
    min_coverage: float = .95
    min_valid_price: float = .95
    max_latency: float = 15

    def observe(self, batch):
        self.coverage_ratio, self.valid_price_ratio = batch.coverage_ratio, batch.valid_price_ratio
        self.latency, self.timestamp_advanced = batch.latency, batch.timestamp_advanced
        usable = sum(batch.snapshots[s].quote_status in {"LIVE", "CACHED"}
                     for s in batch.returned_symbols)
        fresh = usable / max(1, len(batch.returned_symbols)) >= self.min_coverage
        if batch.market_session in {"open", "auction"}:
            fresh = fresh and batch.timestamp_advanced is not False
        good = (bool(batch.valid_symbols) and self.coverage_ratio >= self.min_coverage
                and self.valid_price_ratio >= self.min_valid_price
                and self.latency <= self.max_latency and fresh
                and any(q.quote_status != "UNAVAILABLE" for q in batch.snapshots.values()))
        self.last_sample_good = good
        if good:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            self.reason = "healthy_sample"
            if self.consecutive_successes >= self.recover_after:
                self.state = "HEALTHY"
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            self.reason = "coverage_price_latency_or_freshness_failed"
            if self.consecutive_failures >= self.unavailable_after:
                self.state = "UNAVAILABLE"
            elif self.consecutive_failures >= self.degrade_after:
                self.state = "DEGRADED"


class Freshness:
    def __init__(self, max_age_seconds=15, calendar=DEFAULT_TRADING_CALENDAR):
        self.previous = {}
        self._day = None
        self.max_age_seconds, self.calendar = max_age_seconds, calendar

    def status(self, quote, now):
        now = to_beijing(now)
        stamp = quote.quote_time
        if quote.price is None or quote.price <= 0 or stamp is None:
            return "UNAVAILABLE", None
        session = market_session(now, self.calendar)
        day = now.date()
        if session == "pre_open":
            day -= timedelta(days=1)
        while not self.calendar.is_trading_day(day):
            day -= timedelta(days=1)
        if self._day != now.date():
            self.previous.clear()
            self._day = now.date()
        if stamp > now + timedelta(seconds=2):
            return "UNAVAILABLE", False
        key = (quote.symbol, day, session, now.hour >= 13)
        previous = self.previous.get(key)
        advanced = None if previous is None else stamp > previous
        if previous is None or stamp > previous:
            self.previous[key] = stamp
        if stamp.date() != day:
            return "STALE", advanced
        if session not in {"open", "auction"}:
            return "CACHED", advanced
        if (now - stamp).total_seconds() > self.max_age_seconds:
            return "STALE", advanced
        if previous is None:
            return "CACHED", None  # Baseline, not a claimed advancing live tick.
        return ("LIVE" if advanced else "STALE"), advanced
