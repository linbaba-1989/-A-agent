"""Presentation-only workforce adapters; no model calls or schema changes."""
import math
import re
from collections.abc import Mapping
from ui.view_models import analysis_mode_display

ROLES = {"technical_analyst": "技术分析", "fundamental_event_analyst": "基本面 / 事件",
         "sentiment_analyst": "市场情绪", "risk_officer": "风险官", "chief_researcher": "总研究员"}
DATA_LABELS = {"available": "可用", "partial": "部分可用", "unavailable": "不可用"}
TECH_FIELDS = ("last_price", "recent_daily_k", "ma5", "ma10", "ma20", "ma60", "atr14")
FUND_FIELDS = ("fundamental_data", "event_data", "announcement_data", "news_data", "industry_data")
ROLE_FIELDS = {"technical_analyst": TECH_FIELDS, "fundamental_event_analyst": FUND_FIELDS,
               "sentiment_analyst": ("sentiment_external_data", "news_data", "volume_ratio",
                                     "turnover_rate", "speed_1m", "speed_3m", "speed_5m"),
               "risk_officer": TECH_FIELDS + ("security_status", "fundamental_data", "event_data",
                                               "sentiment_external_data")}


def present(value):
    if value is None: return False
    if isinstance(value, str): return value.strip().lower() not in ("", "--", "unavailable", "unknown", "nan", "none")
    if isinstance(value, (float, int)): return math.isfinite(value)
    if isinstance(value, dict):
        if value.get("data_status") == "unavailable": return False
        return any(present(v) for k, v in value.items() if k != "data_status")
    if isinstance(value, (list, tuple)): return any(present(v) for v in value)
    return False


def result_rows(result):
    result = result if isinstance(result, Mapping) else {}
    employees = result.get("employees")
    employees = employees if isinstance(employees, Mapping) else {}
    return {**{role: row if isinstance(row, Mapping) else {} for role, row in employees.items()},
            "chief_researcher": result.get("chief_researcher") if isinstance(result.get("chief_researcher"), Mapping) else {}}


_CHIEF_ALIASES = {"short_term_view": "short_term", "mid_term_view": "mid_term",
                  "trend_state": "trend", "risk_level": "risk"}
_OPTIONAL_LISTS = ("key_drivers", "key_risks", "key_conflicts", "invalidation_conditions",
                   "missing_evidence", "evidence")


def adapt_research_result(result):
    """Normalize saved report shapes for UI only; never infer a missing fact."""
    source = result if isinstance(result, Mapping) else {}
    rows = result_rows(source)
    chief_row = rows["chief_researcher"]
    chief_data = chief_row.get("data")
    chief_data = dict(chief_data) if isinstance(chief_data, Mapping) else {}
    for current, legacy in _CHIEF_ALIASES.items():
        if current not in chief_data and legacy in chief_data:
            chief_data[current] = chief_data[legacy]
    for key in _OPTIONAL_LISTS:
        if not isinstance(chief_data.get(key), list):
            chief_data[key] = []
    if "final_summary" not in chief_data and "summary" in chief_data:
        chief_data["final_summary"] = chief_data["summary"]
    employees = {role: {**row, "data": dict(row.get("data")) if isinstance(row.get("data"), Mapping) else {}}
                 for role, row in rows.items() if role != "chief_researcher"}
    return {**source, "employees": employees,
            "chief_researcher": {**chief_row, "data": chief_data}}


def data_status(role, facts, row=None):
    data = (row or {}).get("data") or {}
    data = data if isinstance(data, Mapping) else {}
    facts = facts if isinstance(facts, Mapping) else None
    values = [present((facts or {}).get(key)) for key in ROLE_FIELDS[role]]
    status = "available" if all(values) else "partial" if any(values) else "unavailable"
    explicit = data.get("data_status")
    # Old reports without facts require an explicit status; success is not data evidence.
    if facts is None and explicit in DATA_LABELS:
        status = explicit
    elif explicit in DATA_LABELS:
        status = max((status, explicit), key=lambda x: list(DATA_LABELS).index(x))
    if status == "available" and any(data.get(k) for k in (
            "data_gaps", "missing_data", "missing_sentiment_data", "missing_information")):
        status = "partial"
    return status


def workforce_data(result):
    result = result if isinstance(result, Mapping) else {}
    rows = result_rows(result)
    statuses = {role: data_status(role, result.get("fact_data"), rows.get(role)) for role in ROLE_FIELDS}
    values = list(statuses.values())
    overall = "available" if all(v == "available" for v in values) else (
        "unavailable" if all(v == "unavailable" for v in values) else "partial")
    chief = rows["chief_researcher"]
    if overall == "available" and ((chief.get("data") or {}).get("data_gaps") or
                                    (chief.get("data") or {}).get("missing_roles") or chief.get("status") == "degraded"):
        overall = "partial"
    return {**statuses, "chief_researcher": overall}


def elapsed_display(value):
    try:
        number = float(value)
        return f"{number:.1f}s" if math.isfinite(number) and number >= 0 else "--"
    except (ValueError, TypeError): return "--"


def model_display(value):
    if not value: return "--"
    names = {"deepseek-v4-pro": "DeepSeek V4-Pro", "qwen3.8-max": "Qwen 3.8 MAX",
             "qwen3.7-plus": "Qwen 3.7 Plus", "kimi-k3": "Kimi K3"}
    return re.sub(r"\bmax\b", analysis_mode_display("max"), names.get(value, str(value)), flags=re.I)


def execution_state(state, row):
    if row:
        if row.get("success") is True: return "完成"
        if row.get("success") is False:
            return "超时" if row.get("timeout_stage") or "timeout" in str(row.get("error", "")).lower() else "失败"
    return {"working": "运行中", "running": "运行中", "error": "失败", "failed": "失败",
            "complete": "完成", "completed": "完成"}.get(state, "等待")


def configured_models(ctx):
    """Read existing routes; do not choose, alter, or invoke a model."""
    import os
    result = {}
    for role, route in ctx.get("routes", {}).items():
        candidate = (route.get("candidates") or [{"provider": route.get("primary")}])[0]
        model = candidate.get("model") or os.getenv(candidate.get("model_env") or "", "")
        if not model and ctx.get("registry") and candidate.get("provider"):
            model = ctx["registry"].get(candidate["provider"]).model_name
        result[role] = model
    return result


def chief_summary(result):
    chief = adapt_research_result(result)["chief_researcher"]["data"]
    # The headline is a direct rendering of ChiefReport, including older saved
    # reports that did not yet have these fields. Do not infer a missing value.
    stance = chief.get("overall_view")
    short_term = chief.get("short_term_view")
    mid_term = chief.get("mid_term_view")
    trend = chief.get("trend_state")
    risk = chief.get("risk_level")
    view_labels = {"bullish": "偏多", "neutral_bullish": "中性偏多", "neutral": "中性",
                   "neutral_bearish": "中性偏空", "bearish": "偏空", "unavailable": "数据不足"}
    trend_labels = {"strong_up": "强势上行", "uptrend": "上升趋势", "range_up": "偏强震荡",
                    "range": "区间震荡", "range_down": "偏弱震荡", "downtrend": "下降趋势",
                    "strong_down": "强势下行", "unclear": "趋势不清晰", "unavailable": "数据不足",
                    "up": "上行", "down": "下行"}
    risk_labels = {"low": "低", "medium": "中等", "high": "高", "very_high": "极高",
                   "unavailable": "数据不足"}
    completeness = {"complete": "完整", "partial": "部分缺失",
                    "insufficient": "证据不足"}.get(chief.get("data_completeness"))
    return {"stance": view_labels.get(stance, stance) if present(stance) else None,
            "short_term": view_labels.get(short_term, short_term) if present(short_term) else None,
            "mid_term": view_labels.get(mid_term, mid_term) if present(mid_term) else None,
            "trend": trend_labels.get(trend, trend) if present(trend) else None,
            "risk": risk_labels.get(risk, risk) if present(risk) else None,
            "confidence": chief.get("confidence") if isinstance(chief.get("confidence"), (int, float)) else None,
            "confidence_cap": chief.get("confidence_cap") if isinstance(chief.get("confidence_cap"), (int, float)) else None,
            "data_status": completeness}


def chief_conclusions(result):
    """Only existing ChiefReport conclusion fields, normalized for compact display."""
    chief = adapt_research_result(result)["chief_researcher"]["data"]

    def strings(key, text_key=None):
        values = chief.get(key)
        if not isinstance(values, list): return []
        items = []
        for value in values:
            text = value.get(text_key) if isinstance(value, dict) and text_key else value
            if isinstance(text, str) and present(text): items.append(text.strip())
        return items

    return {"关键驱动": strings("key_drivers"),
            "关键风险": strings("key_risks"),
            "关键冲突": strings("key_conflicts", "description"),
            "失效条件": strings("invalidation_conditions", "condition"),
            "缺失证据": strings("missing_evidence")}
