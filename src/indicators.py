"""Transparent technical indicators calculated exclusively from QMT data."""
from __future__ import annotations

from typing import Any
import pandas as pd

UNAVAILABLE = "unavailable"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if pd.notna(number) else None
    except (TypeError, ValueError):
        return None


def percent_change(current: Any, base: Any) -> float | str:
    current_value, base_value = _number(current), _number(base)
    if current_value is None or base_value in (None, 0):
        return UNAVAILABLE
    return round((current_value / base_value - 1) * 100, 4)


def moving_average(frame: pd.DataFrame, window: int) -> float | str:
    if "close" not in frame or len(frame) < window:
        return UNAVAILABLE
    values = pd.to_numeric(frame["close"], errors="coerce").dropna()
    return round(float(values.tail(window).mean()), 4) if len(values) >= window else UNAVAILABLE


def rolling_high(frame: pd.DataFrame, window: int, exclude_last: bool = False) -> float | str:
    if "high" not in frame:
        return UNAVAILABLE
    values = pd.to_numeric(frame["high"], errors="coerce").dropna()
    if exclude_last:
        values = values.iloc[:-1]
    return round(float(values.tail(window).max()), 4) if len(values) >= window else UNAVAILABLE


def speed(frame: pd.DataFrame, minutes: int, current: Any) -> float | str:
    if "close" not in frame or len(frame) < minutes:
        return UNAVAILABLE
    values = pd.to_numeric(frame["close"], errors="coerce").dropna()
    return percent_change(current, values.iloc[-minutes]) if len(values) >= minutes else UNAVAILABLE


def vwap(frame: pd.DataFrame) -> float | str:
    if not {"amount", "volume"}.issubset(frame.columns):
        return UNAVAILABLE
    amount = pd.to_numeric(frame["amount"], errors="coerce").sum()
    volume = pd.to_numeric(frame["volume"], errors="coerce").sum()
    value = amount / (volume * 100) if volume else 0  # QMT A-share volume is normally lots.
    return round(float(value), 4) if value > 0 else UNAVAILABLE


def intraday_position(last: Any, low: Any, high: Any) -> float | str:
    last_value, low_value, high_value = _number(last), _number(low), _number(high)
    if None in (last_value, low_value, high_value) or high_value == low_value:
        return UNAVAILABLE
    return round((last_value - low_value) / (high_value - low_value) * 100, 2)


def volume_ratio(today_volume: Any, daily: pd.DataFrame) -> float | str:
    current = _number(today_volume)
    if current is None or "volume" not in daily or len(daily) < 6:
        return UNAVAILABLE
    history = pd.to_numeric(daily["volume"], errors="coerce").dropna()
    baseline = history.iloc[-6:-1].mean()
    return round(current / baseline, 4) if baseline > 0 else UNAVAILABLE
