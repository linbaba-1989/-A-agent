"""A-share market clock and quote freshness policy.

Market session is derived from the exchange calendar and the Beijing clock.
It must never be inferred from the timestamp of the last quote: a provider
can be disconnected, paused, or serving an old cached snapshot while the
exchange is already open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from threading import RLock
from zoneinfo import ZoneInfo


try:
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - only used on hosts without tzdata
    BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


PRE_OPEN = "pre_open"
AUCTION = "auction"
OPEN = "open"
LUNCH_BREAK = "lunch_break"
CLOSED = "closed"
MARKET_SESSIONS = (PRE_OPEN, AUCTION, OPEN, LUNCH_BREAK, CLOSED)
QUOTE_STATUSES = ("LIVE", "STALE", "CACHED", "UNAVAILABLE")

# Five default (2s) full-market polling cycles, not a single unchanged frame.
DEFAULT_QUOTE_POLL_SECONDS = 2
QUOTE_GRACE_CYCLES = 5
QUOTE_GRACE_SECONDS = DEFAULT_QUOTE_POLL_SECONDS * QUOTE_GRACE_CYCLES
MIN_STALE_FRESH_FETCHES = 2


class QuoteEvidence:
    """One market-level evidence ledger per feed; readers never count fetches."""

    def __init__(self):
        self._lock = RLock()
        self.session_key = None
        self.fresh_fetch_count = 0
        self.consecutive_non_advance_count = 0
        self.last_advanced_at = None
        self.first_fetched_at = None
        self.last_fetched_at = None
        self.quote_timestamp = None
        self.high_watermark = None
        self.valid_quote_count = 0
        self.last_seq = -1

    @staticmethod
    def key(session, clock):
        return (clock.date(), session, clock.hour >= 13)

    def observe(self, timestamp, valid_count, seq, session, now=None):
        clock = to_beijing(now)
        with self._lock:
            key = self.key(session, clock)
            if key != self.session_key:
                self.session_key = key
                self.fresh_fetch_count = self.consecutive_non_advance_count = 0
                self.first_fetched_at = self.last_advanced_at = None
                self.last_fetched_at = None
                self.quote_timestamp = None
                self.high_watermark = None
                self.last_seq = -1
            if seq <= self.last_seq:
                return
            self.last_seq = seq
            self.fresh_fetch_count += 1
            self.last_fetched_at = clock
            self.valid_quote_count = valid_count
            stamp = timestamp_seconds(timestamp)
            self.first_fetched_at = self.first_fetched_at or clock
            if stamp is not None and (self.high_watermark is None or stamp > self.high_watermark):
                self.last_advanced_at = clock
                self.high_watermark = stamp
                self.consecutive_non_advance_count = 0
            else:
                self.consecutive_non_advance_count += 1
            self.quote_timestamp = stamp

    def status(self, session, now=None):
        clock = to_beijing(now)
        with self._lock:
            quote = timestamp_to_beijing(self.quote_timestamp)
            if not self.valid_quote_count or quote is None:
                return "UNAVAILABLE"
            if session not in {OPEN, AUCTION}:
                return "CACHED"
            if self.key(session, clock) != self.session_key:
                return "CACHED"  # A fresh session probe is still required.
            if not _quote_belongs_to_current_session(quote, session, clock):
                return "STALE"
            if (clock - quote).total_seconds() > QUOTE_GRACE_SECONDS:
                return "STALE"  # Explicit stale exchange-time evidence, not missing previous.
            if (self.fresh_fetch_count >= MIN_STALE_FRESH_FETCHES
                    and self.consecutive_non_advance_count
                    and self.last_advanced_at is not None
                    and (clock - self.last_advanced_at).total_seconds() >= QUOTE_GRACE_SECONDS):
                return "STALE"
            return "LIVE"

    def stop_eligible(self, session, now=None):
        clock = to_beijing(now)
        with self._lock:
            return (self.status(session, clock) == "STALE"
                    and self.key(session, clock) == self.session_key
                    and self.fresh_fetch_count >= MIN_STALE_FRESH_FETCHES
                    and self.first_fetched_at is not None
                    and self.last_fetched_at is not None
                    and (self.last_fetched_at - self.first_fetched_at).total_seconds() >= QUOTE_GRACE_SECONDS
                    and self.status(session, self.last_fetched_at) == "STALE")

_OPENING_AUCTION_START = time(9, 15)
_CONTINUOUS_OPEN_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)
_CLOSING_TIME = time(15, 0)


def _dates(values: Iterable[str]) -> frozenset[date]:
    return frozenset(date.fromisoformat(value) for value in values)


# SSE/SZSE's published 2026 A-share full-day closures.  The calendar is
# injectable so deployments can replace this finite built-in schedule with a
# maintained exchange calendar without changing the session policy.
DEFAULT_NON_TRADING_DAYS = _dates(
    (
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04",
        "2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
        "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
        "2026-02-28",
        "2026-04-04", "2026-04-05", "2026-04-06",
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-05-09",
        "2026-06-19", "2026-06-20", "2026-06-21",
        "2026-09-20", "2026-09-25", "2026-09-26", "2026-09-27",
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
        "2026-10-06", "2026-10-07", "2026-10-10",
    )
)


def _coerce_dates(values: Iterable[date | str]) -> frozenset[date]:
    result: set[date] = set()
    for value in values:
        if isinstance(value, datetime):
            result.add(value.date())
        elif isinstance(value, date):
            result.add(value)
        else:
            result.add(date.fromisoformat(str(value)))
    return frozenset(result)


@dataclass(frozen=True)
class AShareTradingCalendar:
    """Small exchange-calendar adapter used by the UI clock.

    Weekdays are trading days unless explicitly closed.  ``extra_trading_days``
    supports an exchange calendar that publishes weekend make-up sessions.
    Both arguments accept ``date`` objects or ISO date strings.
    """

    non_trading_days: frozenset[date] = field(default_factory=lambda: DEFAULT_NON_TRADING_DAYS)
    extra_trading_days: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "non_trading_days", _coerce_dates(self.non_trading_days))
        object.__setattr__(self, "extra_trading_days", _coerce_dates(self.extra_trading_days))

    def is_trading_day(self, value: date | datetime) -> bool:
        day = value.date() if isinstance(value, datetime) else value
        if day in self.extra_trading_days:
            return True
        return day.weekday() < 5 and day not in self.non_trading_days


DEFAULT_TRADING_CALENDAR = AShareTradingCalendar()


def to_beijing(moment: datetime | None = None) -> datetime:
    """Normalize an aware/naive datetime to Asia/Shanghai.

    Naive test and application datetimes are interpreted as Beijing time.  A
    timestamp returned by a provider is handled separately by
    :func:`timestamp_to_beijing`.
    """

    value = moment or datetime.now(BEIJING_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def timestamp_to_beijing(value: Any) -> datetime | None:
    """Parse provider/audit timestamps without using the host timezone."""

    if isinstance(value, datetime):
        return to_beijing(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number <= 0:
            return None
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=BEIJING_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "unavailable":
            return None
        try:
            return timestamp_to_beijing(float(text))
        except ValueError:
            pass
        try:
            return to_beijing(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def timestamp_seconds(value: Any) -> float | None:
    moment = timestamp_to_beijing(value)
    return moment.timestamp() if moment else None


def market_session(moment: datetime | None = None,
                   calendar: AShareTradingCalendar = DEFAULT_TRADING_CALENDAR) -> str:
    """Return the exchange session for the local/Beijing clock."""

    current = to_beijing(moment)
    if not calendar.is_trading_day(current.date()):
        return CLOSED
    clock = current.time()
    if clock < _OPENING_AUCTION_START:
        return PRE_OPEN
    if clock < _CONTINUOUS_OPEN_START:
        return AUCTION
    if clock < _MORNING_END:
        return OPEN
    if clock < _AFTERNOON_START:
        return LUNCH_BREAK
    if clock < _CLOSING_TIME:
        return OPEN
    return CLOSED


def should_fetch_quotes(session: str) -> bool:
    """Whether a quote request is allowed for a market session."""

    return session in {AUCTION, OPEN}


def should_high_frequency_refresh(session: str) -> bool:
    return session == OPEN


def market_session_label(session: str | None) -> str:
    return {
        PRE_OPEN: "开盘前",
        AUCTION: "集合竞价",
        OPEN: "交易中",
        LUNCH_BREAK: "午休",
        CLOSED: "已收盘",
    }.get(session or "", "--")


def _quote_belongs_to_current_session(moment: datetime, session: str, clock: datetime) -> bool:
    if moment.date() != clock.date() or moment > clock:
        return False
    quote_clock = moment.time()
    if session == AUCTION:
        return _OPENING_AUCTION_START <= quote_clock < _CONTINUOUS_OPEN_START
    if session == OPEN:
        if clock.time() < _MORNING_END:
            return _CONTINUOUS_OPEN_START <= quote_clock < _MORNING_END
        return _AFTERNOON_START <= quote_clock < _CLOSING_TIME
    return False


def quote_status(previous_timestamp: Any, current_timestamp: Any, session: str,
                 now: datetime | None = None, *, evidence: QuoteEvidence | None = None) -> str:
    """Classify quote freshness independently from market session.

    Live consumers pass shared fresh-fetch evidence, including bootstrap grace.
    Timestamp-only compatibility callers cannot establish fresh-fetch provenance;
    a recent unproven quote is CACHED, never bootstrap LIVE.
    """

    if evidence is not None:
        return evidence.status(session, now)
    current = timestamp_to_beijing(current_timestamp)
    if current is None:
        return "UNAVAILABLE"
    clock = to_beijing(now)
    if session in {AUCTION, OPEN} and not _quote_belongs_to_current_session(current, session, clock):
        return "STALE"
    if session in {AUCTION, OPEN}:
        previous = timestamp_to_beijing(previous_timestamp)
        if previous is None and (clock - current).total_seconds() <= QUOTE_GRACE_SECONDS:
            return "CACHED"  # Timestamp alone is not proof of a fresh provider fetch.
        if (previous is not None and _quote_belongs_to_current_session(previous, session, clock)
                and current.timestamp() > previous.timestamp()):
            return "LIVE"
        return "STALE"
    return "CACHED"
