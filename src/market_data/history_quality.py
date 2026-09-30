"""Historical validation with explicit, evidence-based missing observations."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
from ..market_clock import AShareTradingCalendar

PERIODS = {"1d": "bars_daily", "1m": "bars_1m", "5m": "bars_5m", "15m": "bars_15m", "30m": "bars_30m"}
PRICES = ["open", "high", "low", "close", "volume", "amount"]


def moment(value, end=False):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    if isinstance(value, date) and not isinstance(value, datetime) or isinstance(value, str) and len(value) == 10:
        if end:
            stamp += pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return stamp.to_pydatetime()


class HistoricalCalendar:
    """Reuse the application's calendar adapter, with a bounded verified schedule.

    Do not extrapolate the built-in 2026 holidays back to 2024/2025.
    """
    def __init__(self, sessions, start, end, source="xtdc"):
        self.start, self.end = moment(start).date(), moment(end).date()
        self.sessions = frozenset(moment(x).date() for x in sessions)
        if not self.sessions or any(x < self.start or x > self.end for x in self.sessions):
            raise ValueError("invalid_calendar_window")
        days = [self.start + timedelta(days=i) for i in range((self.end-self.start).days+1)]
        self.adapter = AShareTradingCalendar(non_trading_days=frozenset(set(days)-self.sessions),
            extra_trading_days=frozenset(x for x in self.sessions if x.weekday() >= 5))
        self.source = source
        self._grids = {}

    def grid(self, period):
        if period not in self._grids:
            days = np.array(sorted(self.sessions), dtype='datetime64[ns]')
            if period == '1d':
                values = days
            else:
                step = int(period[:-1])
                minutes = list(range(570+step,691,step))+list(range(780+step,901,step))
                if step == 1:minutes.insert(0,570)
                values = (days[:,None]+np.array(minutes,dtype='timedelta64[m]')[None,:]).reshape(-1)
            self._grids[period] = pd.DatetimeIndex(values)
        return self._grids[period]

    def between(self, start, end):
        first, last = moment(start).date(), moment(end).date()
        if first < self.start or last > self.end:
            raise ValueError("calendar_range_unverified")
        return sorted(x for x in self.sessions if first <= x <= last)


@dataclass
class QualityReport:
    frame: pd.DataFrame
    gaps: list[dict]
    invalid_rows: int
    duplicate_rows: int
    reverse_pairs: int
    reasons: dict

    @property
    def status(self):
        return "PARTIAL" if self.invalid_rows or self.reverse_pairs or any(g['classification'] != 'EXPECTED_MISSING' for g in self.gaps) else "VALID"


def validate_frame(frame, period, start, end, calendar, listing_date=None):
    if period not in PERIODS:
        raise ValueError("unsupported_history_period")
    start, end = moment(start), moment(end, True)
    if start > end:
        raise ValueError("invalid_date_range")
    sessions = calendar.between(start, end)
    f = frame.copy().reset_index(drop=True)
    if f.empty:
        f = pd.DataFrame(columns=["timestamp", *PRICES])
        f['timestamp'] = pd.to_datetime(f['timestamp'])
    elif 'time' in f:
        f['timestamp'] = pd.to_datetime(pd.to_numeric(f['time'], errors='coerce'), unit='ms', utc=True).dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    elif 'timestamp' in f:
        f['timestamp'] = pd.to_datetime(f['timestamp'], errors='coerce')
        if f['timestamp'].dt.tz is not None:
            f['timestamp'] = f['timestamp'].dt.tz_convert('Asia/Shanghai').dt.tz_localize(None)
    else:
        raise ValueError('missing_timestamp')
    if not set(PRICES) <= set(f):
        raise ValueError('missing_ohlcv_fields')
    f[PRICES] = f[PRICES].apply(pd.to_numeric, errors='coerce')
    ts = f['timestamp']; mins = ts.dt.hour*60 + ts.dt.minute
    duplicate = ts.duplicated(keep='first')
    reverse = int((ts.diff().dt.total_seconds() < 0).sum())
    outside = ((mins < 570) | (mins > 900) | ((mins > 690) & (mins < 780))) if period != '1d' else (mins != 0)
    step = 1 if period == '1d' else int(period[:-1])
    off_grid = (ts.dt.second != 0) | (ts.dt.microsecond != 0) | (ts.dt.nanosecond != 0)
    if period != '1d':
        off_grid |= (mins % step != 0) | ((mins == 570) & (step != 1)) | (mins == 780)
    checks = dict(invalid_timestamp=ts.isna(), duplicate=duplicate,
        nonfinite=pd.Series(~np.isfinite(f[PRICES].to_numpy(dtype=float)).all(axis=1), index=f.index),
        invalid_ohlc=(f.low > f.open) | (f.open > f.high) | (f.low > f.close) | (f.close > f.high) | (f[['open','high','low','close']] <= 0).any(axis=1),
        negative_volume=f.volume < 0, negative_amount=f.amount < 0,
        outside_request=(ts < start) | (ts > end), outside_session=outside,
        non_trading_day=~ts.dt.date.isin(sessions), off_grid=off_grid)
    bad = pd.DataFrame(checks).any(axis=1)
    clean = f.loc[~bad, ['timestamp', *PRICES]].sort_values('timestamp').copy()
    clean['trade_date'] = clean.timestamp.dt.date
    listed = moment(listing_date).date() if listing_date else None
    gaps=[]
    grid = calendar.grid(period)
    expected = grid[(grid>=start)&(grid<=end)]
    missing = expected.difference(pd.DatetimeIndex(clean.timestamp))
    if len(missing):
        missing_frame = pd.DataFrame({'timestamp':missing,'date':missing.date})
        for day, group in missing_frame.groupby('date'):
            known = listed is not None and day < listed
            gaps.append(dict(trade_date=day,first_missing=group.timestamp.min().to_pydatetime(),last_missing=group.timestamp.max().to_pydatetime(),
                missing_count=len(group),classification='EXPECTED_MISSING' if known else 'UNKNOWN_MISSING',
                reason='BEFORE_VERIFIED_LISTING_DATE' if known else 'UNKNOWN'))
    return QualityReport(clean,gaps,int(bad.sum()),int(duplicate.sum()),reverse,{k:int(v.sum()) for k,v in checks.items() if v.any()})
