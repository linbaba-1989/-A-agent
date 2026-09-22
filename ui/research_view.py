"""Presentation-only workforce adapters; no model calls or schema changes."""
import math
import re
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
    return {**(result.get("employees") or {}), "chief_researcher": result.get("chief_researcher") or {}}


def data_status(role, facts, row=None):
    data = (row or {}).get("data") or {}
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
    chief = (result.get("chief_researcher") or {}).get("data") or {}
    employees = result.get("employees") or {}
    def field(role, key):
        row = employees.get(role) or {}
        return (row.get("data") or {}).get(key) if row.get("success") else None
    # Never infer stance from prose or bull/bear list lengths.
    stance = chief.get("overall_view") or chief.get("stance")
    trend, risk = field("technical_analyst", "trend"), field("risk_officer", "risk_level")
    trend_labels = {"strong_up": "强势上涨", "up": "上涨", "down": "下跌", "strong_down": "强势下跌",
                    "sideways": "震荡", "neutral": "中性"}
    risk_labels = {"low": "低", "medium": "中等", "high": "高"}
    return {"stance": stance if present(stance) else None,
            "trend": trend_labels.get(trend, trend) if present(trend) else None,
            "risk": risk_labels.get(risk, risk) if present(risk) else None,
            "confidence": chief.get("confidence"),
            "data_status": {"available": "可用", "partial": "部分缺失", "unavailable": "不可用"}[
                workforce_data(result)["chief_researcher"]]}
