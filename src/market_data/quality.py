"""Per-source hysteresis and per-symbol, source-time-based freshness."""
from dataclasses import dataclass
from datetime import timedelta
from ..market_clock import DEFAULT_TRADING_CALENDAR, market_session, to_beijing


def provider_failure_reasons(*, coverage, valid_ratio, latency, has_prices, advanced,
                             session, has_recent_quotes=True, errors=(),
                             min_coverage=.95, min_valid_price=.95, max_latency=15):
    """Transport/completeness and market progression, not the fraction of liquid stocks."""
    reasons = []
    if any(e.startswith("HTTP_") for e in errors):
        reasons.append("HTTP_FAILURE")
    if any(e in {"TimeoutError", "URLError", "ConnectionError", "ConnectionResetError", "OSError"} for e in errors):
        reasons.append("NETWORK_FAILURE")
    if coverage < min_coverage:
        reasons.append("COVERAGE_FAILURE")
    if not has_prices or valid_ratio < min_valid_price:
        reasons.append("VALID_PRICE_FAILURE")
        if any("malformed" in e or "parse" in e.lower() for e in errors):
            reasons.append("PARSE_FAILURE")
    if latency > max_latency or "snapshot_budget_exceeded" in errors:
        reasons.append("LATENCY")
    if not has_recent_quotes or (session in {"open", "auction"} and advanced is False):
        reasons.append("TIMESTAMP_STALL")
    return reasons


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
        reasons = provider_failure_reasons(coverage=self.coverage_ratio, valid_ratio=self.valid_price_ratio,
            latency=self.latency, has_prices=bool(batch.valid_symbols), advanced=self.timestamp_advanced,
            session=batch.market_session, has_recent_quotes=usable > 0, errors=batch.errors,
            min_coverage=self.min_coverage, min_valid_price=self.min_valid_price, max_latency=self.max_latency)
        evidence = getattr(batch, "provider_evidence", {})
        active = evidence.get("active_symbols", 0)
        # Price/volume/amount changed: their timestamps should progress coherently.
        # Untraded securities never enter this denominator. Keep the existing
        # completeness threshold; do not lower it to conceal real market stalls.
        if (batch.market_session in {"open", "auction"} and active and
                evidence.get("active_advancing_symbols", 0) / active < self.min_coverage):
            if "TIMESTAMP_STALL" not in reasons:
                reasons.append("TIMESTAMP_STALL")
        good = not reasons
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
            self.reason = ",".join(reasons)
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
