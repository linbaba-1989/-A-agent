"""Daily contracts and quality. No vendor SDK or paid runtime imports."""
from dataclasses import dataclass, field, asdict, replace
from datetime import date, datetime, time, timedelta
import math
import pandas as pd
from .contracts import canonical_symbol
from ..market_clock import DEFAULT_TRADING_CALENDAR, to_beijing

ADJUSTMENTS = {"raw", "qfq", "hfq"}
INDEX_SYMBOLS = {"000001.SH", "399001.SZ", "399006.SZ"}


def completed_date(now=None, calendar=DEFAULT_TRADING_CALENDAR):
    now = to_beijing(now)
    day = now.date()
    # Conservative publication buffer: today's bar eligible only after 18:00.
    if now.time().replace(tzinfo=None) < time(18):
        day -= timedelta(days=1)
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day


@dataclass(frozen=True)
class HistoricalBar:
    symbol: str
    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume_shares: float | None = None
    amount_cny: float | None = None
    prev_close: float | None = None
    pct_change: float | None = None
    source: str = ""
    adjustment: str = "raw"
    data_status: str = "COMPLETE"

    def __post_init__(self):
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol, allow_index=True))
        if self.adjustment not in ADJUSTMENTS:
            raise ValueError("invalid_adjustment")
        if not isinstance(self.trade_date, date) or isinstance(self.trade_date, datetime):
            raise ValueError("trade_date_required")

    def to_dict(self):
        return {**asdict(self), "trade_date": self.trade_date.isoformat()}


@dataclass
class HistoryResult:
    symbol: str
    bars: list[HistoricalBar] = field(default_factory=list)
    source: str = ""
    adjustment: str = "raw"
    data_status: str = "UNAVAILABLE"
    quality_status: str = "UNAVAILABLE"
    warnings: list[str] = field(default_factory=list)
    latency: float = 0
    fallback_used: bool = False
    updated_at: str | None = None
    requested_end: date | None = None

    def frame(self):
        # Existing history service expects OHLC, volume/amount and a date index.
        rows = [b.to_dict() for b in self.bars]
        frame = pd.DataFrame(rows, columns=list(HistoricalBar.__dataclass_fields__))
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame["trade_date"]), name="date")
        frame["volume"] = frame["volume_shares"]
        frame["amount"] = frame["amount_cny"]
        frame.attrs.update(volume_unit="shares", amount_unit="CNY", source=self.source,
                           adjustment=self.adjustment, data_status=self.data_status,
                           quality_status=self.quality_status, warnings=list(self.warnings))
        return frame

    def metrics(self):
        return dict(symbol=self.symbol, provider=self.source, row_count=len(self.bars),
                    start_date=str(self.bars[0].trade_date) if self.bars else None,
                    end_date=str(self.bars[-1].trade_date) if self.bars else None,
                    latency=self.latency, adjustment=self.adjustment,
                    fallback_used=self.fallback_used, data_status=self.data_status,
                    quality_status=self.quality_status, warnings=self.warnings)


def validate(bars, symbol, source, adjustment, start, end, now=None,
             calendar=DEFAULT_TRADING_CALENDAR):
    result = HistoryResult(symbol, source=source, adjustment=adjustment, requested_end=end)
    cutoff = min(end, completed_date(now, calendar))
    unique, severe = {}, False
    last_input = None
    for bar in bars:
        if (bar.symbol, bar.source, bar.adjustment) != (symbol, source, adjustment):
            result.warnings.append("identity_or_adjustment_mismatch")
            severe = True
            continue
        day = bar.trade_date
        if day > to_beijing(now).date():
            result.warnings.append("future_date:" + str(day))
            severe = True
            continue
        if day > cutoff:
            result.warnings.append("unfinished_daily_excluded:" + str(day))
            continue
        if day < start:
            continue
        if not calendar.is_trading_day(day):
            result.warnings.append("non_trading_date:" + str(day))
            severe = True
            continue
        if last_input and day < last_input:
            result.warnings.append("input_not_sorted")
        last_input = day
        ohlc = [bar.open, bar.high, bar.low, bar.close]
        numbers = ohlc + [bar.volume_shares, bar.amount_cny, bar.prev_close, bar.pct_change]
        if (any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                   or not math.isfinite(v)) for v in numbers)
            or any(v is None for v in ohlc)
            or not bar.low <= bar.open <= bar.high
            or not bar.low <= bar.close <= bar.high
            or any(v is not None and v < 0 for v in (bar.volume_shares, bar.amount_cny))):
            result.warnings.append("invalid_bar:" + str(day))
            severe = True
            continue
        if day in unique:
            result.warnings.append("duplicate_last_valid_wins:" + str(day))
        unique[day] = replace(bar, data_status="COMPLETE")
    result.bars = [unique[d] for d in sorted(unique)]
    if not result.bars:
        return result
    # Gaps are recorded, never synthesized; suspension/listing needs separate metadata.
    expected = result.bars[0].trade_date
    while expected <= cutoff:
        if calendar.is_trading_day(expected) and expected not in unique:
            result.warnings.append("missing_session:" + str(expected))
        expected += timedelta(days=1)
    gaps = any(w.startswith("missing_session:") for w in result.warnings)
    result.quality_status = "DEGRADED" if severe or gaps else "VALID"
    result.data_status = "PARTIAL" if severe or gaps else "COMPLETE"
    return result


def moving_averages(result):
    from ..indicators import moving_average
    if (result.data_status not in {"COMPLETE", "PARTIAL", "CACHED"} or result.quality_status != "VALID"
        or len({(b.source, b.adjustment) for b in result.bars}) > 1):
        return {f"MA{n}": "unavailable" for n in (5, 10, 20, 60)}
    frame = result.frame()
    return {f"MA{n}": moving_average(frame, n) for n in (5, 10, 20, 60)}


def daily_volume_comparison(snapshot, result, now=None):
    # Require exact previous trading session, not last available stale bar.
    now = to_beijing(now)
    day = now.date() - timedelta(days=1)
    while not DEFAULT_TRADING_CALENDAR.is_trading_day(day):
        day -= timedelta(days=1)
    base = next((b for b in reversed(result.bars) if b.trade_date == day), None)
    current = snapshot.volume_shares
    valid = (base is not None and base.symbol == snapshot.symbol
             and snapshot.quote_time is not None
             and snapshot.quote_time.date() == now.date()
             and snapshot.quote_status in {"LIVE", "CACHED"}
             and result.quality_status == "VALID"
             and current is not None and current >= 0
             and base.volume_shares is not None and base.volume_shares > 0)
    return {"today_volume_shares": current,
            "previous_day_volume_shares": base.volume_shares if base else None,
            "daily_volume_multiple": current / base.volume_shares if valid else None,
            "status": "available" if valid else "unavailable"}
