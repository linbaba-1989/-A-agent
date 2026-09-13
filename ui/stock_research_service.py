"""Read-only adapter for the stock research terminal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

import pandas as pd

from src.indicators import UNAVAILABLE, percent_change
from src.market_cache import (daily_suspend_flag, history_indicators, realtime_ma,
                              security_status, validated_turnover)


def _number(value: Any) -> float | None:
    try:
        return None if value in (None, UNAVAILABLE) or pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


def range_position(current: Any, low: Any, high: Any) -> float | str:
    current_value, low_value, high_value = map(_number, (current, low, high))
    if current_value is None or low_value is None or high_value is None or high_value == low_value:
        return UNAVAILABLE
    return round((current_value - low_value) / (high_value - low_value) * 100, 2)


def trend_label(last: Any, ma5: Any, ma10: Any, ma20: Any) -> str:
    values = list(map(_number, (last, ma5, ma10, ma20)))
    if any(value is None for value in values):
        return "中性"
    price, avg5, avg10, avg20 = values
    if price > avg20 and avg5 > avg10 > avg20:
        return "短线偏强"
    if price < avg20 and avg5 < avg10 < avg20:
        return "短线偏弱"
    return "中性"


def market_speed(value: Any, market_status: str) -> Any:
    return value if market_status == "open" else UNAVAILABLE


def toggle_watchlist(watchlist: list[str], symbol: str) -> bool:
    if symbol in watchlist:
        watchlist.remove(symbol)
        return False
    watchlist.append(symbol)
    return True


def select_research_symbol(session: dict[str, Any], symbol: str) -> None:
    session["selected_symbol"] = symbol
    session["nav_page"] = "个股研究"


def _history_dates(frame: pd.DataFrame) -> pd.Series:
    index = pd.Series(frame.index, index=frame.index)
    text = index.astype(str).str.replace(r"\.0$", "", regex=True)
    trading_dates = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if trading_dates.notna().sum() >= max(1, len(frame) // 2):
        return trading_dates
    if "time" in frame:
        values = pd.to_numeric(frame["time"], errors="coerce")
        return pd.to_datetime(values, unit="ms", errors="coerce")
    return trading_dates


def prepare_history(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result["date"] = _history_dates(result).values
    result = result.dropna(subset=["date"])
    for field in ("open", "high", "low", "close", "volume", "amount"):
        if field in result:
            result[field] = pd.to_numeric(result[field], errors="coerce")
    return result.sort_values("date").reset_index(drop=True)


@dataclass
class StockResearchSnapshot:
    symbol: str
    quote: dict[str, Any]
    facts: dict[str, Any]
    history: pd.DataFrame
    load_seconds: float


class StockResearchService:
    def __init__(self, provider: Any, scanner: Any, public_status: dict[str, Any]):
        self.provider = provider
        self.scanner = scanner
        self.public_status = public_status

    def load(self, symbol: str, history_count: int = 250) -> StockResearchSnapshot:
        started = perf_counter()
        ticks = self.provider.get_full_ticks([symbol])
        tick = ticks.get(symbol)
        if not tick or not self.provider._valid_tick(tick):
            raise LookupError("quote_unavailable")
        detail = self.provider.normalized_instrument(symbol)
        timestamp = self.provider.tick_timestamp(tick)
        as_of = datetime.fromtimestamp(timestamp) if timestamp else None
        raw_history = self.provider.get_history([symbol], "1d", history_count).get(symbol)
        history = prepare_history(raw_history)
        indicators = history_indicators(raw_history, as_of)
        closes = indicators.get("closes", [])
        last = tick.get("lastPrice", UNAVAILABLE)
        turnover = validated_turnover(tick, detail)
        recent20 = history.tail(20)
        high20 = recent20["high"].max() if "high" in recent20 and not recent20.empty else UNAVAILABLE
        low20 = recent20["low"].min() if "low" in recent20 and not recent20.empty else UNAVAILABLE
        averages = {f"ma{window}": realtime_ma(closes, last, window) for window in (5, 10, 20, 60)}
        atr = indicators.get("atr14", UNAVAILABLE)
        atr_pct = (round(float(atr) / float(last) * 100, 2)
                   if _number(atr) is not None and _number(last) not in (None, 0) else UNAVAILABLE)
        previous = tick.get("lastClose", UNAVAILABLE)
        amplitude = (round((float(tick["high"]) - float(tick["low"])) / float(previous) * 100, 2)
                     if _number(previous) not in (None, 0) and _number(tick.get("high")) is not None
                     and _number(tick.get("low")) is not None else UNAVAILABLE)
        quote_time = self.provider.normalize_tick(symbol, tick)["timestamp"]
        market_status = "open" if self.public_status.get("market") == "交易中" else "closed"
        facts = {
            "code": symbol, "name": detail.get("name") or UNAVAILABLE, "quote_time": quote_time,
            "market_status": market_status, "last_price": last, "previous_close": previous,
            "open": tick.get("open", UNAVAILABLE), "high": tick.get("high", UNAVAILABLE),
            "low": tick.get("low", UNAVAILABLE), "volume": tick.get("volume", UNAVAILABLE),
            "amount": tick.get("amount", UNAVAILABLE), "change": (_number(last) - _number(previous))
            if _number(last) is not None and _number(previous) is not None else UNAVAILABLE,
            "change_pct": percent_change(last, previous), "amplitude": amplitude,
            "turnover_rate": turnover["value"], **averages, "atr14": atr, "atr_pct": atr_pct,
            "high_20d": high20, "low_20d": low20, "range_position_20d": range_position(last, low20, high20),
            "trend": trend_label(last, averages["ma5"], averages["ma10"], averages["ma20"]),
            "breakout_status": "20日新高" if _number(high20) is not None and float(last) >= float(high20)
            else "未确认突破", "volume_ratio": self.scanner._volume_ratio(tick, indicators),
            "security_status": security_status(tick, daily_suspend_flag(raw_history, as_of)),
            "speed_1m": market_speed(self.scanner.snapshot_history.speed(symbol, 1), market_status),
            "speed_3m": market_speed(self.scanner.snapshot_history.speed(symbol, 3), market_status),
            "speed_5m": market_speed(self.scanner.snapshot_history.speed(symbol, 5), market_status),
            "fundamental_data": UNAVAILABLE, "event_data": UNAVAILABLE, "announcement_data": UNAVAILABLE,
            "news_data": UNAVAILABLE, "industry_data": UNAVAILABLE, "sentiment_external_data": UNAVAILABLE,
            "source": "QMT/xtquant",
        }
        quote = {**tick, "timestamp": quote_time, "name": facts["name"],
                 "turnover_rate": facts["turnover_rate"], "amplitude": amplitude}
        return StockResearchSnapshot(symbol, quote, facts, history, perf_counter() - started)
