"""Compact, role-isolated status cards for the AI research workspace."""
import html
import re

import streamlit as st

from ui.research_view import DATA_LABELS, ROLES, elapsed_display, execution_state, model_display, workforce_data


ROLE_NAMES = {
    "technical_analyst": "技术分析员",
    "fundamental_event_analyst": "基本面分析员",
    "sentiment_analyst": "情绪分析员",
    "risk_officer": "风险官",
    "chief_researcher": "总研究员",
}
ROLE_DATA_NAMES = {
    "technical_analyst": "技术行情",
    "fundamental_event_analyst": "基本面 / 事件",
    "sentiment_analyst": "市场情绪",
    "risk_officer": "风险证据",
    "chief_researcher": "综合数据",
}
STATE_COLORS = {"等待": "#667085", "运行中": "#175cd3", "完成": "#067647",
                "失败": "#b54708", "超时": "#b54708"}


def _case_count(audit: dict) -> int:
    """The selected IDs are the auditable source for the visible count."""
    return len(audit.get("selected_case_ids") or [])


def _case_status(enabled: bool | None, audit: dict) -> str:
    status = audit.get("few_shot_status")
    if status == "disabled" or (enabled is False and not status):
        return "关闭"
    if status == "selected":
        return "开启"
    if status == "no_match":
        return "开启 · 未匹配"
    if status == "unavailable":
        return "开启 · 暂不可用"
    if enabled is None:
        return "--"
    return "开启" if enabled else "关闭"


def _case_detail_lines(cases: list[dict]) -> None:
    for case in cases:
        st.markdown(f"**{html.escape(str(case.get('case_id') or '--'))}**")
        st.caption(str(case.get("title") or "--"))
        score = case.get("score")
        try:
            score_text = f"{float(score):.1f}"
        except (TypeError, ValueError):
            score_text = "--"
        st.caption(f"相关度 {score_text}")


def _case_detail_html(cases: list[dict]) -> str:
    rows = []
    for case in cases:
        try:
            score = f"{float(case.get('score')):.1f}"
        except (TypeError, ValueError):
            score = "--"
        rows.append("<div class='role-case'><b>" + html.escape(str(case.get("case_id") or "--"))
                    + "</b><span>" + html.escape(str(case.get("title") or "--"))
                    + "</span><strong>相关度 " + score + "</strong></div>")
    return "".join(rows)


def _role_model_display(role: str, value, provider=None) -> str:
    # Doubao uses an opaque deployment ID in the route; the product label is
    # independent of that ID. Keep actual fallback model names visible.
    if role == "sentiment_analyst" and (provider == "doubao" or
                                         isinstance(value, str) and re.fullmatch(r"ep-[A-Za-z0-9-]+", value)):
        return "Doubao"
    return model_display(value)


def render_ai_states(states, results=None, *, facts=None, models=None,
                     few_shot_enabled: bool | None = None, few_shot_audit=None,
                     case_details=None) -> None:
    """Show the five jobs, their input health, and only selected case metadata."""
    results, models = results or {}, models or {}
    few_shot_audit = few_shot_audit or {}
    resolved_cases = case_details if case_details is not None else {}
    data = workforce_data({"fact_data": facts, "employees": results,
                           "chief_researcher": results.get("chief_researcher", {})})
    for col, role in zip(st.columns(5), ROLES):
        row = results.get(role) or {}
        status = execution_state(states.get(role), row)
        model = _role_model_display(role, row.get("actual_model") or row.get("model") or models.get(role),
                                    row.get("provider"))
        if row.get("fallback") is True:
            model += "（备用）"
        audit = few_shot_audit.get(role) or {}
        with col.container(border=True):
            lines = [f"<div class='role-workbench'><b class='role-name'>{html.escape(ROLE_NAMES[role])}</b>",
                     f"<div class='role-model'>{html.escape(model)}</div>",
                     f"<div class='role-row' style='color:{STATE_COLORS[status]}'>状态：{html.escape(status)}</div>",
                     f"<div class='role-row'>数据：{html.escape(ROLE_DATA_NAMES[role] + DATA_LABELS[data[role]])}</div>",
                     f"<div class='role-row'>案例增强：{html.escape(_case_status(few_shot_enabled, audit))}</div>"]
            if few_shot_enabled and not audit:
                lines.append("<div class='role-row'>预计案例：动态选择</div>")
            lines.append(f"<div class='role-row'>耗时：{elapsed_display(row.get('latency'))}</div>")
            if row.get("success") and data[role] != "available":
                lines.append("<div class='role-row role-limited'>基于有限数据生成</div>")
            st.markdown("".join(lines) + "</div>", unsafe_allow_html=True)
            if audit:
                cases = resolved_cases.get(role) or []
                count = _case_count(audit)
                with st.expander(f"参考案例：{count}个"):
                    if count:
                        if cases:
                            _case_detail_lines(cases)
                            if len(cases) < count:
                                st.caption("部分案例明细暂不可用")
                        else:
                            st.caption("案例明细暂不可用")
                    else:
                        st.caption("本次未选中案例")


def render_case_preview(preview: dict[str, dict]) -> None:
    """Display local retrieval predictions, never as completed model work."""
    st.markdown("**案例匹配预览**")
    st.caption("根据当前 Fact Bundle 本地匹配；仅供预览，不调用 AI 模型，不计费。")
    for col, role in zip(st.columns(5), ROLES):
        row = preview.get(role) or {}
        cases = row.get("cases") or []
        with col.container(border=True):
            st.markdown("<div class='role-workbench'><b class='role-name'>"
                        + html.escape(ROLE_NAMES[role]) + "</b><div class='role-row'>预计案例："
                        + str(len(cases)) + "个</div>"
                        + (_case_detail_html(cases) if cases else
                           "<div class='role-row role-limited'>当前场景暂无匹配</div>")
                        + "</div>", unsafe_allow_html=True)
