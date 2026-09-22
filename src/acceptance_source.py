"""Source policy and acceptance entry checks, independent of quote/LIVE policy."""
from dataclasses import dataclass, replace
from datetime import time
from threading import RLock

from .market_clock import DEFAULT_TRADING_CALENDAR, OPEN, market_session, to_beijing

TOKEN_SOURCE = "XtDataCenter Token"
MIN_ACCEPTANCE_SECONDS = 600


@dataclass(frozen=True)
class SourceRequest:
    preferred_source: str = "auto"
    acceptance_source: str | None = None


def acceptance_mode(state, environ):
    """Explicit developer opt-in; saved UI choice overrides the startup default."""
    return bool(state.get("acceptance_mode", environ.get("A_AGENT_ACCEPTANCE_MODE") == "1"))


def source_request(state, environ):
    # Neither a legacy source lock nor allocation tracing opts users into tests.
    lock = "xtdatacenter" if acceptance_mode(state, environ) else None
    preferred = "xtdatacenter" if lock else state.get("market_source", "auto")
    if preferred not in {"auto", "xtdatacenter", "qmt"}:
        raise ValueError("unsupported_market_source")
    return SourceRequest(preferred, lock)


class SourceResources:
    """One active source/feed. Close old streaming consumer BEFORE its provider.

    Cached by Streamlit as one owner, rather than caching a live provider for
    each source choice. A rerun with identical policy does not initialize again.
    """
    def __init__(self, router_factory, scanner_factory, feed_factory):
        self.router_factory = router_factory
        self.scanner_factory = scanner_factory
        self.feed_factory = feed_factory
        self.lock = RLock()
        self.request = self.router = self.selection = self.scanner = self.feed = None
        self.streaming_service = None
        self.generation = 0

    def activate(self, request):
        with self.lock:
            if request == self.request:
                return self
            if (request.acceptance_source == "xtdatacenter" and self.selection is not None
                    and self.selection.name == TOKEN_SOURCE and self.selection.status == "connected"):
                # Entering acceptance from an already connected Token source
                # changes policy, not the provider/feed instance or init count.
                self.selection = replace(self.selection, required_source=TOKEN_SOURCE,
                                         fallback_enabled=False, fallback=False,
                                         qmt_fallback_status="disabled", token_configured=True)
                self.router.selection = self.selection
                self.request = request
                self.generation += 1
                return self
            self.close()
            self.router = self.router_factory()
            self.selection = self.router.select(acceptance_source=request.acceptance_source,
                                                preferred_source=request.preferred_source)
            if self.selection.provider is not None:
                self.scanner = self.scanner_factory(self.selection.provider)
                self.feed = self.feed_factory(self.selection.provider, self.scanner)
            self.request = request
            self.generation += 1
            return self

    def close(self):
        with self.lock:
            self.stop_stream()
            if self.router is not None:
                # Drain an in-flight request before releasing the old source.
                if self.feed is not None:
                    with self.feed._lock:
                        self.router.close()
                else:
                    self.router.close()
            self.request = self.router = self.selection = self.scanner = self.feed = None

    def stop_stream(self):
        with self.lock:
            if self.streaming_service is not None:
                self.streaming_service.close()
                self.streaming_service = None

    def verify_acceptance_connection(self):
        """Check the existing Token connection at Start; never select/fallback."""
        with self.lock:
            if (self.request is None or self.request.acceptance_source != "xtdatacenter"
                    or self.selection is None or self.selection.provider is None):
                return False
            provider = self.selection.provider
            reason = "xtdc_unavailable"
            try:
                if hasattr(provider, "backend") and provider.backend is None:
                    ok = False  # check_connection would initialize again: do not do that.
                    reason = "xtdc_worker_unavailable"
                else:
                    ok = bool(provider.check_connection().ok)
            except Exception as exc:
                ok = False
                reason = f"xtdc_check_failed:{type(exc).__name__}"
            if ok:
                return True
            # A failed test probe must not tear down production resources/cache.
            return False


def continuous_seconds_remaining(now, calendar=DEFAULT_TRADING_CALENDAR):
    clock = to_beijing(now)
    if not calendar.is_trading_day(clock):
        return 0
    # SSE stock continuous auction ends 14:57; this gate does NOT change the
    # application's existing market_session or quote-status implementation.
    for start, end in ((time(9, 30), time(11, 30)), (time(13), time(14, 57))):
        if start <= clock.time() < end:
            return max(0, (clock.replace(hour=end.hour, minute=end.minute, second=0,
                                         microsecond=0) - clock).total_seconds())
    return 0


def worker_count():
    """Read-only count in this app's process tree; never starts a worker."""
    import psutil
    try:
        return sum(any(arg.endswith("xtdc_worker.py") for arg in child.cmdline())
                   for child in psutil.Process().children(recursive=True))
    except (psutil.Error, OSError):
        return None


def acceptance_preflight(selection, feed, *, now=None, calendar=DEFAULT_TRADING_CALENDAR,
                         workers=None):
    clock = to_beijing(now)
    remaining = continuous_seconds_remaining(clock, calendar)
    active = selection.name if selection.provider is not None else "unavailable"
    source_reasons = []
    if selection.required_source != TOKEN_SOURCE:
        source_reasons.append("ACCEPTANCE_LOCK_REQUIRED")
    if not selection.token_configured:
        source_reasons.append("XTDC_NOT_CONFIGURED")
    if selection.provider is None or selection.status != "connected":
        source_reasons.append("XTDC_UNAVAILABLE")
    if active != TOKEN_SOURCE:
        source_reasons.append("ACTIVE_SOURCE_MISMATCH")
    if selection.fallback_enabled or selection.fallback or selection.qmt_fallback_status != "disabled":
        source_reasons.append("QMT_FALLBACK_NOT_DISABLED")
    if feed is None or feed.provider is not selection.provider:
        source_reasons.append("SHARED_FEED_SOURCE_MISMATCH")
    reasons = list(source_reasons)
    session = market_session(clock, calendar)
    if not calendar.is_trading_day(clock):
        reasons.append("NOT_TRADING_DAY")
    if session != OPEN:
        reasons.append("MARKET_NOT_OPEN")
    if remaining < MIN_ACCEPTANCE_SECONDS:
        reasons.append("CONTINUOUS_TIME_LT_10_MIN")
    initializations = getattr(feed, "provider_initializations", None)
    if type(initializations) is not int:
        reasons.append("PROVIDER_INIT_COUNT_UNAVAILABLE")
    elif initializations != 1:
        reasons.append("PROVIDER_INIT_COUNT_NOT_ONE")
    if type(workers) is not int:
        reasons.append("WORKER_COUNT_UNAVAILABLE")
    elif workers != 1:
        reasons.append("WORKER_COUNT_NOT_ONE")
    return {"status": "SOURCE_BLOCKED" if source_reasons else "PREFLIGHT_BLOCKED" if reasons else "READY",
            "ready": not reasons, "reasons": reasons, "required_source": TOKEN_SOURCE,
            "active_source": active, "fallback": "DISABLED" if not (
                selection.fallback_enabled or selection.fallback or selection.qmt_fallback_status != "disabled") else "ENABLED",
            "market_session": session, "remaining_continuous_seconds": remaining,
            "provider_init_count": initializations, "worker_count": workers,
            "checked_at": clock.isoformat()}


def shared_beta_feed(ctx):
    """No source selection here: only identity-checked access to the owner feed."""
    feed, selection = ctx.get("realtime_feed"), ctx["selection"]
    owner = ctx.get("source_resources")
    if (feed is None or selection.name != TOKEN_SOURCE or selection.provider is not feed.provider
            or (owner is not None and (owner.feed is not feed or owner.selection is not selection))):
        return None
    return feed


def acceptance_running(grant, preflight, generation, now=None):
    if not grant or grant.get("generation") != generation:
        return False
    if to_beijing(now).timestamp() >= grant["ends_at"]:
        return False
    # The ten-minute remaining-time test is an entry condition, not a reason
    # to abort an already-started ten-minute run nine minutes before close.
    remaining = preflight["remaining_continuous_seconds"]
    blockers = [reason for reason in preflight["reasons"]
                if reason != "CONTINUOUS_TIME_LT_10_MIN"]
    return not blockers and remaining > 0
