"""Pure UI adapters. They never fetch data or change calculation semantics."""
from __future__ import annotations

from typing import Any

from src.market_clock import (DEFAULT_TRADING_CALENDAR, MARKET_SESSIONS, OPEN, QUOTE_STATUSES,
                              market_session, market_session_label,
                              quote_status as quote_freshness_status, timestamp_seconds,
                              timestamp_to_beijing, to_beijing)


UNAVAILABLE = "unavailable"
RANGE_PLACEHOLDERS = ("最低", "最高")
FUNDAMENTAL_GAP_MESSAGE = "当前未接入完整基本面 / 公告 / 新闻数据"


def normalize_stock_name(value: Any, fallback: str = "--") -> str:
    """Return a Unicode stock name without re-encoding an existing valid string."""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        text = ""
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                text = raw.decode(encoding).strip()
                break
            except UnicodeDecodeError:
                continue
    else:
        text = ""
    if not text or "\ufffd" in text:
        return fallback or "--"
    return text


def normalize_symbol(value: str) -> str:
    text = value.strip().upper()
    if text.isdigit() and len(text) == 6:
        return text + (".SH" if text.startswith(("5", "6", "9")) else ".SZ")
    return text


def cn_change_color(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "#7a8494"
    return "#d92d20" if number > 0 else "#079455" if number < 0 else "#7a8494"


def market_label(status: str | None) -> str:
    if status in {"pre_open", "auction", "open", "lunch_break", "closed"}:
        return market_session_label(status)
    return "交易中" if status == "open" else "已收盘" if status == "closed" else "--"


ANALYSIS_MODE_LABELS = {"standard": "标准", "deep": "深度", "max": "MAX"}


def analysis_mode_display(value: str | None) -> str:
    return ANALYSIS_MODE_LABELS.get(value or "", "--")


def analysis_mode_value(label: str | None) -> str:
    return {display: value for value, display in ANALYSIS_MODE_LABELS.items()}.get(label or "", "standard")


def history_status_display(value: str | None) -> str:
    return {"not_started": "尚未开始", "initializing": "正在初始化", "ready": "已就绪",
            "partial": "部分可用", "failed": "初始化失败"}.get(value or "", "--")


def display_value(value: Any, suffix: str = "") -> str:
    if value is None or value == UNAVAILABLE:
        return "--"
    return f"{value}{suffix}"


def format_number(value: Any, decimals: int = 2) -> str:
    try:
        if value is None or value == UNAVAILABLE: return "--"
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "--"


def format_amount(value: Any) -> str:
    try: number = float(value)
    except (TypeError, ValueError): return "--"
    if abs(number) >= 100_000_000: return f"{number / 100_000_000:.2f}亿"
    if abs(number) >= 10_000: return f"{number / 10_000:.2f}万"
    return f"{number:.2f}元"


def format_security_status(value: Any) -> str:
    return {"normal": "正常", "unknown": "未知", "suspended": "停牌",
            "resumed_today": "今日复牌", "invalid_quote": "行情无效"}.get(value, "--")


def safe_error(error: str | None) -> tuple[str, str | None]:
    if not error:
        return "运行失败", None
    lowered = error.lower()
    if "schema_validation_failed" in lowered or "validation" in lowered or "pydantic" in lowered:
        return "模型返回结构异常，系统已尝试修复", error
    if "timeout" in lowered:
        return "模型响应超时", error
    if "market_data_unavailable" in lowered or "qmt" in lowered or "xtdc" in lowered:
        return "行情服务不可用", error
    return "服务暂时不可用", error


def role_state(value: str | None) -> str:
    return {"working": "运行中", "idle": "等待", "fallback": "降级",
            "error": "失败", "complete": "完成", "degraded": "降级"}.get(value or "", "等待")


def _quote_time_text(timestamp: Any) -> str:
    moment = timestamp_to_beijing(timestamp)
    return moment.replace(tzinfo=None).isoformat(timespec="seconds") if moment else UNAVAILABLE


def quote_status_display(status: dict[str, Any] | None, current_timestamp: Any = None,
                         previous_timestamp: Any = None, now=None,
                         market_session_value: str | None = None,
                         quote_status_value: str | None = None,
                         feed=None,
                         calendar=DEFAULT_TRADING_CALENDAR) -> dict[str, Any]:
    """Build the one market/quote status object used by every UI surface.

    The market session may be supplied by a live fragment when a session
    boundary occurs; otherwise it is taken from the already clock-derived
    status.  Quote freshness is never inferred from wall-clock time alone.
    """

    updated = dict(status or {})
    current = to_beijing(now)
    session = market_session_value or updated.get("market_session")
    if session not in MARKET_SESSIONS:
        session = market_session(current, calendar)
    existing = updated.get("quote_status")
    current_seconds = timestamp_seconds(current_timestamp)
    if feed is not None:
        evidence = feed.quote_evidence
        current_seconds = evidence.quote_timestamp
        state = quote_freshness_status(previous_timestamp, current_seconds, session, current,
                                       evidence=evidence)
        updated["valid_quotes"] = evidence.valid_quote_count
        updated["last_quote_timestamp"] = current_seconds
        updated["last_quote_time"] = _quote_time_text(current_seconds)
        updated["fresh_fetch_count"] = evidence.fresh_fetch_count
        updated["stale_stop_eligible"] = evidence.stop_eligible(session, current)
    elif quote_status_value in QUOTE_STATUSES:
        state = quote_status_value
    elif current_timestamp is None and previous_timestamp is None and existing in QUOTE_STATUSES:
        state = existing
    else:
        state = quote_freshness_status(previous_timestamp, current_seconds, session, current)
    updated["market_session"] = session
    updated["market"] = market_session_label(session)
    updated["market_status"] = "open" if session == OPEN else "closed"
    updated["quote_status"] = state
    if current_seconds is not None:
        updated["last_quote_timestamp"] = current_seconds
        updated["last_quote_time"] = _quote_time_text(current_seconds)
    return updated


def public_market_status(selection: Any, audit: dict[str, Any] | None,
                         now=None, calendar=DEFAULT_TRADING_CALENDAR) -> dict[str, Any]:
    """Build public status from the Beijing exchange clock plus quote evidence.

    ``audit.market_status`` is deliberately ignored for market-session
    decisions.  It is a persisted quote-era snapshot and can be several days
    old when the application starts.
    """

    audit = audit or {}
    current = to_beijing(now)
    session = market_session(current, calendar)
    last_quote_time = audit.get("latest_quote_time", UNAVAILABLE)
    last_quote_timestamp = timestamp_seconds(audit.get("latest_quote_timestamp", last_quote_time))
    base = {"provider": selection.name, "provider_status": selection.status,
            "market": market_session_label(session), "market_session": session,
            "market_status": "open" if session == "open" else "closed",
            "quote_status": None, "last_quote_time": last_quote_time,
            "last_quote_timestamp": last_quote_timestamp,
            "raw_universe": audit.get("raw_count", UNAVAILABLE),
            "active_universe": audit.get("active_count", UNAVAILABLE),
            "valid_quotes": audit.get("valid_tick_count", UNAVAILABLE),
            "qmt_fallback": selection.qmt_fallback_status,
            "token_configured": selection.name == "XtDataCenter Token" or selection.status != "failed"}
    return quote_status_display(base, current_timestamp=last_quote_timestamp,
                                now=current, market_session_value=session)


def apply_realtime_quote_status(status: dict[str, Any], current_timestamp: Any,
                                previous_timestamp: Any = None, now=None) -> dict[str, Any]:
    """Compatibility wrapper for the unified :func:`quote_status_display`."""

    return quote_status_display(status, current_timestamp=current_timestamp,
                                previous_timestamp=previous_timestamp, now=now,
                                market_session_value=status.get("market_session"))


def report_view(result: dict[str, Any]) -> dict[str, Any]:
    from ui.research_view import adapt_research_result, chief_summary, workforce_data
    adapted = adapt_research_result(result)
    summary = chief_summary(adapted)
    return {**summary, "data_levels": workforce_data(adapted),
            "chief_row": adapted["chief_researcher"],
            "chief": adapted["chief_researcher"]["data"],
            "employees": adapted["employees"], "raw": result}


def raw_json_expanded_default() -> bool:
    return False


def filter_scan_rows(rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    def number(row, key):
        value = row.get("lastPrice") if key == "last_price" and row.get(key) is None else row.get(key)
        try: return float(value)
        except (TypeError, ValueError): return None
    result = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        market = filters.get("market", "全部")
        if market == "沪市" and not symbol.endswith(".SH"): continue
        if market == "深市" and not symbol.endswith(".SZ"): continue
        if market == "北交所" and not symbol.endswith(".BJ"): continue
        rejected = False
        for key, low_key, high_key in (("last_price", "price_min", "price_max"),
                                        ("change_pct", "change_min", "change_max"),
                                        ("turnover_rate", "turnover_min", "turnover_max")):
            value = number(row, key)
            low, high = filters.get(low_key), filters.get(high_key)
            if low is not None and (value is None or value < low): rejected = True
            if high is not None and (value is None or value > high): rejected = True
        for key, filter_key in (("amount", "amount_min"), ("speed_1m", "speed_1m"),
                                ("speed_3m", "speed_3m"), ("speed_5m", "speed_5m")):
            threshold = filters.get(filter_key)
            value = number(row, key)
            if threshold is not None and (value is None or value < threshold): rejected = True
        for window in (5, 10, 20, 60):
            if filters.get(f"above_ma{window}"):
                price, ma = number(row, "last_price"), number(row, f"ma{window}")
                if price is None or ma is None or price < ma: rejected = True
        if filters.get("recent_high"):
            last, high = number(row, "last_price"), number(row, "high_20d")
            if last is None or high is None or last < high: rejected = True
        if filters.get("ma_breakout"):
            crossed = any(number(row, "last_price") is not None and number(row, f"ma{w}") is not None
                          and number(row, "last_price") > number(row, f"ma{w}")
                          and number(row, "lastClose") is not None and number(row, f"previous_ma{w}") is not None
                          and number(row, "lastClose") <= number(row, f"previous_ma{w}") for w in (5, 10, 20, 60))
            if not crossed: rejected = True
        if not rejected: result.append(row)
    return result
