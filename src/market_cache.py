"""Long-lived static/history caches and short-lived real-time tick history."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

import pandas as pd

from .indicators import UNAVAILABLE


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def normalize_instrument(code: str, detail: dict[str, Any]) -> dict[str, Any]:
    def share_count(*keys: str):
        value = _first(detail, *keys)
        try:
            number = float(value)
            # Reject corrupt ABI/RPC decodes and sentinel values.
            return number if 100_000 <= number <= 10_000_000_000_000 else None
        except (TypeError, ValueError):
            return None

    floating = share_count("FloatVolume", "floatVolume")
    total = share_count("TotalVolume", "totalVolume")
    if floating is None or total is None or total < floating or floating <= 1_000_000:
        floating = total = None
    return {
        "code": code,
        "name": _first(detail, "InstrumentName", "instrumentName", "name") or "",
        "float_volume": floating,
        "total_volume": total,
        "volume_multiple": _first(detail, "VolumeMultiple", "volumeMultiple"),
        "is_trading": _first(detail, "IsTrading", "isTrading"),
        "instrument_status": _first(detail, "InstrumentStatus", "instrumentStatus"),
        "open_date": _first(detail, "OpenDate", "openDate") or "",
        "up_limit": _first(detail, "UpStopPrice", "UpLimitPrice", "upStopPrice"),
        "down_limit": _first(detail, "DownStopPrice", "DownLimitPrice", "downStopPrice"),
    }


def infer_volume_unit(samples: list[tuple[dict[str, Any], dict[str, Any]]]) -> tuple[str | None, float | None, list[dict[str, Any]]]:
    """Infer which tick field represents shares from amount/price scale; never guess."""
    evidence: list[dict[str, Any]] = []
    candidates: dict[tuple[str, float], list[float]] = defaultdict(list)
    for tick, instrument in samples:
        try:
            price, amount = float(tick["lastPrice"]), float(tick["amount"])
            if price <= 0 or amount <= 0:
                continue
            item = {"lastPrice": price, "amount": amount, "FloatVolume": instrument.get("float_volume")}
            for field in ("volume", "pvolume"):
                raw = float(tick.get(field) or 0)
                if raw <= 0:
                    continue
                ratio = amount / raw / price
                item[f"amount_per_{field}_vs_price"] = ratio
                if 0.5 <= ratio <= 2:
                    candidates[(field, 1.0)].append(abs(ratio - 1))
                if 50 <= ratio <= 200:
                    candidates[(field, 100.0)].append(abs(ratio / 100 - 1))
            evidence.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    ranked = [(median(errors), -len(errors), field, multiplier) for (field, multiplier), errors in candidates.items()]
    if not ranked:
        return None, None, evidence
    _, negative_count, field, multiplier = min(ranked)
    if -negative_count < 2:  # Require agreement from at least two real samples.
        return None, None, evidence
    return field, multiplier, evidence


def validated_turnover(tick: dict[str, Any], instrument: dict[str, Any], tolerance: float = 0.01) -> dict[str, Any]:
    """Cross-check QMT lots and shares fields before exposing turnover."""
    try:
        float_volume = float(instrument["float_volume"])
        by_volume = float(tick["volume"]) * 100 / float_volume * 100
        by_pvolume = float(tick["pvolume"]) / float_volume * 100
        difference = abs(by_volume - by_pvolume)
        allowed = max(tolerance, max(abs(by_volume), abs(by_pvolume)) * 0.01)
        valid = float_volume > 0 and by_volume >= 0 and by_pvolume >= 0 and difference <= allowed
        return {"value": by_pvolume if valid else UNAVAILABLE,
                "status": "verified" if valid else "turnover_validation_failed",
                "volume_formula": by_volume, "pvolume_formula": by_pvolume,
                "difference": difference}
    except (KeyError, TypeError, ValueError):
        return {"value": UNAVAILABLE, "status": "turnover_validation_failed",
                "volume_formula": UNAVAILABLE, "pvolume_formula": UNAVAILABLE, "difference": UNAVAILABLE}


def turnover_rate(tick: dict[str, Any], instrument: dict[str, Any], field: str | None = None,
                  multiplier: float | None = None):
    return validated_turnover(tick, instrument)["value"]


def security_status(tick: dict[str, Any] | None, suspend_flag: Any) -> str:
    if not tick:
        return "invalid_quote"
    try:
        if float(tick.get("lastClose") or 0) <= 0 or float(tick.get("lastPrice") or 0) <= 0:
            return "invalid_quote"
    except (TypeError, ValueError):
        return "invalid_quote"
    try:
        flag = int(suspend_flag)
    except (TypeError, ValueError):
        return "unknown"
    return {0: "normal", 1: "suspended", -1: "resumed_today"}.get(flag, "unknown")


class SnapshotHistory:
    def __init__(self, retention_seconds: int = 600):
        self.retention_seconds = retention_seconds
        self._values: dict[str, deque[dict[str, float]]] = defaultdict(deque)
        self._sessions: dict[str, tuple[str, str]] = {}

    @staticmethod
    def trading_session(timestamp: float) -> tuple[str, str] | None:
        moment = datetime.fromtimestamp(timestamp)
        clock = moment.time()
        from datetime import time
        if time(9, 30) <= clock <= time(11, 30):
            return moment.date().isoformat(), "morning"
        if time(13, 0) <= clock <= time(15, 0):
            return moment.date().isoformat(), "afternoon"
        return None

    def update(self, code: str, timestamp: float | None, tick: dict[str, Any]) -> None:
        if timestamp is None:
            return
        session = self.trading_session(timestamp)
        if session is None:  # Ignore auction and after-hours ticks.
            return
        try:
            point = {"timestamp": timestamp, "price": float(tick["lastPrice"]),
                     "volume": float(tick.get("volume") or 0), "amount": float(tick.get("amount") or 0)}
        except (KeyError, TypeError, ValueError):
            return
        series = self._values[code]
        if self._sessions.get(code) != session:
            series.clear()
            self._sessions[code] = session
        if not series or timestamp > series[-1]["timestamp"]:
            series.append(point)
        elif timestamp == series[-1]["timestamp"]:
            series[-1] = point
        cutoff = timestamp - self.retention_seconds
        while series and series[0]["timestamp"] < cutoff:
            series.popleft()

    def speed(self, code: str, minutes: int) -> float | str:
        series = self._values.get(code)
        if not series:
            return UNAVAILABLE
        current, target = series[-1], series[-1]["timestamp"] - minutes * 60
        eligible = [point for point in series if point["timestamp"] <= target]
        if not eligible:
            return UNAVAILABLE
        base = max(eligible, key=lambda point: point["timestamp"])
        if base["price"] <= 0:
            return UNAVAILABLE
        return round((current["price"] / base["price"] - 1) * 100, 4)

    def speed_details(self, code: str, minutes: int) -> dict[str, Any] | None:
        series = self._values.get(code)
        if not series:
            return None
        current, target = series[-1], series[-1]["timestamp"] - minutes * 60
        eligible = [point for point in series if point["timestamp"] <= target]
        if not eligible:
            return None
        reference = max(eligible, key=lambda point: point["timestamp"])
        if reference["price"] <= 0:
            return None
        return {"current_time": current["timestamp"], "current_price": current["price"],
                "reference_time": reference["timestamp"], "reference_price": reference["price"],
                "speed": round((current["price"] / reference["price"] - 1) * 100, 4)}


def complete_daily_frame(frame: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Remove today's unfinished daily candle when its date can be identified."""
    if frame.empty:
        return frame.copy()
    today = (now or datetime.now()).date()
    result = frame.copy()
    index_text = pd.Index(result.index).astype(str)
    if all(len(value) == 8 and value.isdigit() for value in index_text):
        parsed = pd.to_datetime(index_text, format="%Y%m%d", errors="coerce")
    else:
        timestamps = result["time"] if "time" in result.columns else result.index
        parsed = pd.to_datetime(timestamps, unit="ms", errors="coerce") if pd.api.types.is_numeric_dtype(timestamps) else pd.to_datetime(timestamps, errors="coerce")
    # Stale quotes can predate the newest locally downloaded candle. Realtime
    # indicators may only use completed candles before the quote trading date.
    mask = pd.Series(parsed, index=result.index).dt.date < today
    return result.loc[mask.to_numpy()].copy()


def daily_suspend_flag(frame: pd.DataFrame, as_of: datetime | None) -> Any:
    if frame.empty or "suspendFlag" not in frame or as_of is None:
        return UNAVAILABLE
    index_text = pd.Index(frame.index).astype(str)
    target = as_of.strftime("%Y%m%d")
    if target in set(index_text):
        value = frame.loc[int(target) if pd.api.types.is_integer_dtype(frame.index) else target, "suspendFlag"]
        return value.iloc[-1] if isinstance(value, pd.Series) else value
    return UNAVAILABLE


def history_indicators(frame: pd.DataFrame, as_of: datetime | None = None) -> dict[str, Any]:
    # Always remove the daily candle matching the current tick's trading date.
    # This also avoids double-counting the last close when scanning after hours.
    complete = complete_daily_frame(frame, as_of)
    close = pd.to_numeric(complete.get("close"), errors="coerce").dropna() if "close" in complete else pd.Series(dtype=float)
    high = pd.to_numeric(complete.get("high"), errors="coerce").dropna() if "high" in complete else pd.Series(dtype=float)
    low = pd.to_numeric(complete.get("low"), errors="coerce").dropna() if "low" in complete else pd.Series(dtype=float)
    result: dict[str, Any] = {"closes": close.tolist(), "previous_ma5": UNAVAILABLE, "previous_ma10": UNAVAILABLE,
                              "previous_ma20": UNAVAILABLE, "previous_ma60": UNAVAILABLE}
    for window in (5, 10, 20, 60):
        if len(close) >= window:
            result[f"previous_ma{window}"] = round(float(close.tail(window).mean()), 4)
    for window in (1, 5, 10, 20):
        result[f"high_{window}d"] = round(float(high.tail(window).max()), 4) if len(high) >= window else UNAVAILABLE
    volumes = pd.to_numeric(complete.get("volume"), errors="coerce").dropna() if "volume" in complete else pd.Series(dtype=float)
    result["avg_volume5"] = round(float(volumes.tail(5).mean()), 4) if len(volumes) >= 5 else UNAVAILABLE
    if len(complete) >= 15 and len(high) == len(low):
        previous_close = pd.to_numeric(complete["close"], errors="coerce").shift(1)
        tr = pd.concat([(pd.to_numeric(complete["high"], errors="coerce") - pd.to_numeric(complete["low"], errors="coerce")),
                        (pd.to_numeric(complete["high"], errors="coerce") - previous_close).abs(),
                        (pd.to_numeric(complete["low"], errors="coerce") - previous_close).abs()], axis=1).max(axis=1)
        result["atr14"] = round(float(tr.tail(14).mean()), 4)
    else:
        result["atr14"] = UNAVAILABLE
    return result


def realtime_ma(closes: list[float], current: Any, window: int):
    try:
        if len(closes) < window - 1:
            return UNAVAILABLE
        return round((sum(closes[-(window - 1):]) + float(current)) / window, 4)
    except (TypeError, ValueError):
        return UNAVAILABLE
