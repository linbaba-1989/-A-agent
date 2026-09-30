"""Streaming zigzag: an extremum becomes observable only after reversal confirmation."""
import numpy as np
import pandas as pd
from .models import BehaviorConfig, Pivot

def prepare_bars(frame, period):
    if period not in ("1d", "30m"):
        raise ValueError("unsupported_period")
    f = frame.copy().reset_index(drop=True)
    required = ["timestamp", "open", "high", "low", "close", "volume", "amount"]
    if not set(required).issubset(f):
        raise ValueError("missing_bar_fields")
    f["timestamp"] = pd.to_datetime(f.timestamp)
    if f.timestamp.duplicated().any() or not f.timestamp.is_monotonic_increasing:
        raise ValueError("bars_must_be_unique_and_ordered")
    values = f[required[1:]].to_numpy(float)
    if not np.isfinite(values).all() or (f[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("invalid_bars")
    if ((f.low > f.open) | (f.low > f.close) | (f.high < f.open) | (f.high < f.close) | (f.volume < 0) | (f.amount < 0)).any():
        raise ValueError("invalid_bars")
    f["available_at"] = f.timestamp.dt.normalize() + pd.Timedelta(hours=15) if period == "1d" else f.timestamp
    return f

def causal_risk(f, config):
    previous = f.close.shift()
    tr = pd.concat([f.high-f.low, (f.high-previous).abs(), (f.low-previous).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/config.atr_window, adjust=False, min_periods=config.atr_window).mean()
    vol = f.close.pct_change().rolling(config.volatility_window).std(ddof=0)
    return atr, vol

class SwingDetector:
    def __init__(self, config=None):
        self.config = config or BehaviorConfig()

    def detect(self, frame, period="1d"):
        f = prepare_bars(frame, period)
        atr, vol = causal_risk(f, self.config)
        high, low, close = (f[k].to_numpy(float) for k in ("high", "low", "close"))
        atr, vol = atr.to_numpy(float), vol.to_numpy(float)
        pivots = []
        mode = None
        high_i = low_i = last_pivot = 0
        for i in range(len(f)):
            if not np.isfinite(atr[i]) or not np.isfinite(vol[i]):
                high_i = low_i = i
                continue
            if high[i] > high[high_i]:
                high_i = i
            if low[i] < low[low_i]:
                low_i = i
            # The reversal scale is frozen at the candidate extremum; no future volatility.
            high_threshold = max(atr[high_i]*self.config.atr_multiple, high[high_i]*vol[high_i]*self.config.volatility_multiple)
            low_threshold = max(atr[low_i]*self.config.atr_multiple, low[low_i]*vol[low_i]*self.config.volatility_multiple)
            down = mode != "down" and i > high_i and high_i-last_pivot >= self.config.min_bars and close[i] <= high[high_i]-high_threshold
            up = mode != "up" and i > low_i and low_i-last_pivot >= self.config.min_bars and close[i] >= low[low_i]+low_threshold
            if down or up:
                # Initial ambiguity is resolved by the most recent extremum, deterministically.
                kind = "HIGH" if down and (not up or high_i > low_i) else "LOW"
                idx = high_i if kind == "HIGH" else low_i
                price = high[idx] if kind == "HIGH" else low[idx]
                pivots.append(Pivot(idx, i, f.timestamp.iloc[idx].isoformat(),
                                    f.available_at.iloc[i].isoformat(), float(price), kind))
                last_pivot = idx
                mode = "down" if kind == "HIGH" else "up"
                high_i = low_i = i
        return pivots
