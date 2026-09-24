"""Compact, schema-led A-share research report."""
import html

import streamlit as st

from ui.research_view import DATA_LABELS, chief_conclusions
from ui.view_models import raw_json_expanded_default, report_view, safe_error


SUMMARY_FIELDS = (("总体观点", "stance"), ("短线观点", "short_term"),
                  ("中期观点", "mid_term"), ("趋势状态", "trend"),
                  ("风险等级", "risk"), ("置信度", "confidence"),
                  ("数据完整度", "data_status"))
DATA_FIELDS = (("技术面", "technical_analyst"), ("基本面/事件", "fundamental_event_analyst"),
               ("市场情绪", "sentiment_analyst"), ("风险数据", "risk_officer"))


def headline_values(view: dict) -> list[tuple[str, str]]:
    """Use Chief schema fields only. Missing historical fields stay --."""
    values = []
    for label, key in SUMMARY_FIELDS:
        value = view.get(key)
        if key == "confidence" and value is not None:
            cap = view.get("confidence_cap")
            value = f"{value} / {cap if cap is not None else '--'}"
        values.append((label, str(value) if value is not None and value != "" else "--"))
    return values


def _tone(raw_view: str | None) -> str:
    if raw_view in {"bullish", "neutral_bullish"}: return "view-up"
    if raw_view in {"bearish", "neutral_bearish"}: return "view-down"
    return ""


def _headline(view: dict) -> str:
    raw_view = (view.get("chief") or {}).get("overall_view")
    cells = []
    for label, value in headline_values(view):
        tone = _tone(raw_view) if label == "总体观点" else ""
        cells.append(f'<div class="research-metric"><span>{html.escape(label)}</span>'
                     f'<strong class="{tone}">{html.escape(value)}</strong></div>')
    return ('<div class="research-block"><div class="research-title">AI 综合研判'
            '<small>Chief Schema · P1.9.1</small></div><div class="research-grid">'
            + "".join(cells) + "</div></div>")


def _data_strip(view: dict) -> str:
    levels = view.get("data_levels") or {}
    items = []
    for name, role in DATA_FIELDS:
        level = levels.get(role, "unavailable")
        label = DATA_LABELS.get(level, "--")
        tone = {"available": "data-ok", "partial": "data-partial",
                "unavailable": "data-unavailable"}.get(level, "")
        items.append(f'<div><span>{html.escape(name)}</span><strong class="{tone}">'
                     f'{html.escape(label)}</strong></div>')
    return ('<div class="research-data"><b>数据状态</b>' + "".join(items) + "</div>")


def _conclusions(result: dict) -> str:
    blocks = []
    for title, values in chief_conclusions(result).items():
        content = "".join(f"<li>{html.escape(value)}</li>" for value in values)
        if not content: content = '<li class="empty">--</li>'
        blocks.append(f'<div class="research-conclusion"><b>{html.escape(title)}</b><ul>{content}</ul></div>')
    return ('<div class="research-block"><div class="research-title">关键结论'
            '<small>直接来自总研究员结构化结果</small></div>'
            '<div class="research-conclusions">' + "".join(blocks) + "</div></div>")


def _list(title: str, values) -> None:
    st.markdown(f"**{title}**")
    if isinstance(values, list) and values:
        for value in values:
            st.markdown(f"- {value}")
    else:
        st.caption("--")


def render_ai_report(result: dict) -> None:
    view = report_view(result)
    chief_row = view.get("chief_row") or {}
    if chief_row.get("success") is False:
        message, detail = safe_error(chief_row.get("error"))
        st.error(message)
        if detail:
            with st.expander("展开开发日志"):
                st.code(detail)
        # A failed Chief must not hide saved specialist results or their statuses.

    st.markdown(_headline(view), unsafe_allow_html=True)
    st.markdown(_data_strip(view), unsafe_allow_html=True)
    st.markdown(_conclusions(result), unsafe_allow_html=True)

    chief = view.get("chief") or {}
    employees = view.get("employees") or {}
    technical = (employees.get("technical_analyst", {}).get("data") or {})
    fundamental = (employees.get("fundamental_event_analyst", {}).get("data") or {})
    sentiment = (employees.get("sentiment_analyst", {}).get("data") or {})
    risk = (employees.get("risk_officer", {}).get("data") or {})
    with st.expander("总研究员说明与岗位明细"):
        st.write(chief.get("final_summary") or "--")
        tabs = st.tabs(["技术面", "基本面 / 事件", "市场情绪", "风险评估"])
        with tabs[0]:
            for label, key in (("趋势", "trend"), ("动量", "momentum"), ("量价", "volume_price"),
                               ("均线", "moving_average_structure"), ("支撑", "support"),
                               ("压力", "resistance"), ("突破", "breakout_status")):
                _list(label, technical.get(key) if isinstance(technical.get(key), list)
                      else [technical.get(key)] if technical.get(key) else [])
        with tabs[1]: st.write(fundamental.get("summary") or "--")
        with tabs[2]: st.write(sentiment.get("summary") or "--")
        with tabs[3]: st.write(risk.get("summary") or "--")
    st.caption("AI分析仅用于研究辅助，不构成投资建议。")
    with st.expander("查看原始分析数据", expanded=raw_json_expanded_default()):
        st.json(view.get("raw") or {})
