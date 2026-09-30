"""Causal indicators and confirmed segments; future labels live elsewhere."""
from dataclasses import asdict
import numpy as np
import pandas as pd
from .models import FEATURES, BehaviorConfig
from .swing_detector import causal_risk, prepare_bars

def indicators(frame, period="1d", config=None):
    config = config or BehaviorConfig()
    f = prepare_bars(frame, period)
    f["atr"], f["volatility"] = causal_risk(f, config)
    for n in (5, 10, 20, 60):
        f["ma"+str(n)] = f.close.rolling(n).mean()
    delta = f.close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    f["rsi"] = np.where(losses > 0, 100-100/(1+gains/losses.replace(0, np.nan)), np.where(gains > 0, 100, 50))
    lo, hi = f.low.rolling(9).min(), f.high.rolling(9).max()
    rsv = ((f.close-lo)/(hi-lo).replace(0, np.nan)*100).fillna(50)
    f["k"] = rsv.ewm(alpha=1/3, adjust=False).mean()
    f["d"] = f.k.ewm(alpha=1/3, adjust=False).mean()
    f["j"] = 3*f.k-2*f.d
    f["macd"] = f.close.ewm(span=12, adjust=False).mean()-f.close.ewm(span=26, adjust=False).mean()
    f["macd_histogram"] = 2*(f.macd-f.macd.ewm(span=9, adjust=False).mean())
    f["volume_ratio"] = f.volume/f.volume.shift().rolling(20).mean().replace(0, np.nan)
    for name in ("volume", "amount"):
        f[name+"_trend"] = f[name].rolling(5).mean()/f[name].shift(5).rolling(5).mean().replace(0, np.nan)-1
    for n in (20, 60):
        f["distance_to_ma"+str(n)] = f.close/f["ma"+str(n)]-1
        # A 30m interval history has eight regular slots per verified trading day.
        f["distance_to_"+str(n)+"d_high"] = f.close/f.high.rolling(n*(8 if period=="30m" else 1)).max()-1
    return f

def build_segments(frame, pivots, period="1d", config=None):
    f = indicators(frame, period, config)
    result = []
    previous_return = previous_duration = 0.0
    for first, last in zip(pivots, pivots[1:]):
        path = f.iloc[first.pivot_index:last.pivot_index+1]
        known = f.iloc[last.confirmation_index]
        closes = path.close.to_numpy()
        direction = 1 if last.pivot_price > first.pivot_price else -1
        features = {key: float(known[key]) for key in FEATURES if key in known.index}
        swing_return = last.pivot_price/first.pivot_price-1
        duration = last.pivot_index-first.pivot_index
        features.update(direction=direction, duration=duration, return_=swing_return,
            max_drawdown=float(np.min(closes/np.maximum.accumulate(closes)-1)),
            max_runup=float(np.max(closes/np.minimum.accumulate(closes)-1)),
            previous_swing_return=previous_return, previous_swing_duration=previous_duration)
        features["return"] = features.pop("return_")
        previous_return, previous_duration = swing_return, duration
        if not all(np.isfinite(features.get(k, np.nan)) for k in FEATURES):
            continue
        result.append(dict(start=first.pivot_time, end=last.pivot_time,
            confirmation_time=last.confirmation_time, confirmation_index=last.confirmation_index,
            start_index=first.pivot_index, end_index=last.pivot_index,
            start_price=first.pivot_price, end_price=last.pivot_price,
            direction="UP" if direction>0 else "DOWN", duration=duration,
            features={k: features[k] for k in FEATURES}, pivot=asdict(last)))
    return result
