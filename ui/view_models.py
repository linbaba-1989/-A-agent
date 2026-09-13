"""Pure UI adapters. They never fetch data or change calculation semantics."""
from __future__ import annotations

from typing import Any


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
    return "交易中" if status == "open" else "已收盘" if status == "closed" else "unavailable"


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


def public_market_status(selection: Any, audit: dict[str, Any] | None) -> dict[str, Any]:
    audit = audit or {}
    return {"provider": selection.name, "provider_status": selection.status,
            "market": market_label(audit.get("market_status")),
            "last_quote_time": audit.get("latest_quote_time", UNAVAILABLE),
            "raw_universe": audit.get("raw_count", UNAVAILABLE),
            "active_universe": audit.get("active_count", UNAVAILABLE),
            "valid_quotes": audit.get("valid_tick_count", UNAVAILABLE),
            "qmt_fallback": selection.qmt_fallback_status,
            "token_configured": selection.name == "XtDataCenter Token" or selection.status != "failed"}


def report_view(result: dict[str, Any]) -> dict[str, Any]:
    chief = result.get("chief_researcher", {})
    data = chief.get("data") or {}
    confidence = data.get("confidence", 0)
    bull, bear = len(data.get("bull_case", [])), len(data.get("bear_case", []))
    stance = "偏多" if bull > bear else "偏空" if bear > bull else "中性"
    return {"stance": stance, "confidence": confidence,
            "data_status": "部分缺失" if data.get("data_gaps") or chief.get("status") == "degraded" else "完整",
            "chief": data, "employees": result.get("employees", {}), "raw": result}


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
