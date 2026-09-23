import html
import os

import streamlit as st

from src.few_shot import load_library
from src.few_shot_features import ROLES
from ui.components.ai_report import render_ai_report
from ui.view_models import analysis_mode_display
from ui.research_view import configured_models, model_display

ARENA_ROLES = {"技术": "technical_analyst", "基本面": "fundamental_event_analyst",
               "情绪": "sentiment_analyst", "风险": "risk_officer", "总研究员": "chief_researcher"}
WORKFORCE_NAMES = {"technical_analyst": "技术分析", "fundamental_event_analyst": "基本面 / 事件",
                   "sentiment_analyst": "市场情绪", "risk_officer": "风险官",
                   "chief_researcher": "总研究员"}
CASE_NAMES = {"technical_analyst": "Technical", "fundamental_event_analyst": "Fundamental",
              "sentiment_analyst": "Sentiment", "risk_officer": "Risk", "chief_researcher": "Chief"}


def capability_snapshot(ctx: dict, session_state: dict | None = None, library=None) -> dict:
    """Read the active route and local case files without making model calls."""
    state = session_state if session_state is not None else st.session_state
    try:
        library = library if library is not None else load_library()
        case_counts = {role: len(library.cases.get(role, ())) for role in ROLES}
        library_errors = bool(library.errors)
    except (OSError, ValueError, TypeError):
        case_counts = {role: 0 for role in ROLES}
        library_errors = True
    routes = ctx.get("routes") or {}
    models = configured_models(ctx)
    model_rows = []
    for role in ROLES:
        candidate = ((routes.get(role) or {}).get("candidates") or [{}])[0]
        provider = candidate.get("provider")
        model = models.get(role)
        if provider == "doubao":
            # The deployment ID is an endpoint identifier, not a user-facing model name.
            displayed_model = "Doubao"
        else:
            displayed_model = model_display(model)
        model_rows.append({"role": role, "name": WORKFORCE_NAMES[role], "model": displayed_model})
    workforce = ctx.get("workforce")
    retriever = getattr(workforce, "few_shot", None)
    default_enabled = bool(getattr(retriever, "enabled", os.getenv("A_SHARE_FEW_SHOT_ENABLED", "0") == "1"))
    enabled = (default_enabled if state.get("ai_case_enhancement_reset_pending")
               else bool(state.get("ai_case_enhancement_enabled", default_enabled)))
    return {"models": model_rows, "case_counts": case_counts, "case_total": sum(case_counts.values()),
            "library_errors": library_errors, "default_enabled": default_enabled,
            "enabled": enabled}


def _table(headers: tuple[str, str], rows: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
                   for row in rows)
    return f"<table class='terminal-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _render_capabilities(ctx: dict) -> None:
    snapshot = capability_snapshot(ctx)
    models, cases, readiness = st.columns([1.2, 1, 1.2], gap="small")
    with models:
        rows = [(row["name"], row["model"]) for row in snapshot["models"]]
        st.markdown("<div class='panel'><div class='side-panel-title'>五位AI员工 · 当前配置</div>"
                    + _table(("岗位", "模型"), rows) + "</div>", unsafe_allow_html=True)
    with cases:
        rows = [(CASE_NAMES[role], str(snapshot["case_counts"][role])) for role in ROLES]
        total = "--" if snapshot["library_errors"] else str(snapshot["case_total"])
        st.markdown("<div class='panel'><div class='side-panel-title'>Few-shot 案例库 · "
                    + html.escape(total) + " 例</div>"
                    + _table(("分类", "案例数"), rows) + "</div>", unsafe_allow_html=True)
    with readiness:
        default_status = "开启" if snapshot["default_enabled"] else "关闭"
        current_status = "开启" if snapshot["enabled"] else "关闭"
        rows = (("Schema", "P1.9.1"), ("Dynamic Few-shot", "P1.9.2"),
                ("真实 A/B 评测", "未开始"), ("动态检索默认", default_status),
                ("动态检索当前", current_status))
        lines = "".join("<div class='ai-line'><span>" + html.escape(label) + "</span><b>"
                        + html.escape(value) + "</b></div>" for label, value in rows)
        st.markdown("<div class='panel'><div class='side-panel-title'>案例库与训练状态</div>"
                    + lines + "</div>", unsafe_allow_html=True)
    if snapshot["library_errors"]:
        st.caption("部分案例文件不可用；案例数只统计已加载内容。")


def arena_table(rows: list[dict], role: str) -> list[dict]:
    selected = [row for row in rows if row.get("role") == role]
    selected.sort(key=lambda row: (row.get("hard_fail", False), -float(row.get("overall_score", 0))))
    return [{"模型": model_display(row.get("model_id")), "综合分": row.get("overall_score", "--"),
             "A股逻辑": row.get("a_share_logic_score", "--"),
             "事实落地": "PASS" if row.get("fact_grounding") else "FAIL",
             "幻觉": row.get("hallucination_count", "--"),
             "Schema": "PASS" if row.get("schema_pass") else "FAIL",
             "延迟": row.get("latency") if row.get("latency") is not None else "--",
             "Token": row.get("total_tokens") if row.get("total_tokens") is not None else "--",
             "成本": row.get("estimated_cost") if row.get("cost_status") != "unknown" else "未知",
             "状态": row.get("status", "--")} for row in selected]


def render(ctx: dict) -> None:
    st.title("AI研究院")
    st.caption("AI研究工作台｜当前岗位配置、案例库与研究结果")
    _render_capabilities(ctx)
    history_tab, arena_tab = st.tabs(["研究记录", "模型竞技场"])
    with history_tab:
        records = st.session_state.get("research_history", [])
        if not records:
            st.caption("暂无正式研究记录。可从个股研究页启动，或先查看本地示例报告。")
        else:
            options = []
            for index, row in enumerate(records):
                chief = row.get("chief_researcher", {})
                confidence = (chief.get("data") or {}).get("confidence", "--")
                options.append(f"{row.get('symbol')}｜{analysis_mode_display(row.get('analysis_mode'))}｜置信度 {confidence}｜{'完成' if chief.get('success') else '失败'}")
            selected = st.selectbox("研究记录", range(len(options)), format_func=lambda index: options[index])
            render_ai_report(records[selected])
        if st.button("预览新版研究报告", key="academy_report_preview"):
            st.session_state["academy_show_example_report"] = not st.session_state.get("academy_show_example_report", False)
        if st.session_state.get("academy_show_example_report", False):
            from ui.pages.stock_research import load_example_report
            st.markdown("<div class='panel'><b>示例预览</b>｜本地保存的示例结果，仅展示报告界面；"
                        "不代表当前股票研究，也不会调用AI模型。</div>", unsafe_allow_html=True)
            render_ai_report(load_example_report())
    with arena_tab:
        st.caption("候选模型使用同一 Fact Bundle、角色 Prompt、Schema 和 Eval；竞技场不会自动切换生产模型。")
        label = st.segmented_control("岗位", list(ARENA_ROLES), default="技术")
        rows = st.session_state.get("model_arena_results", [])
        table = arena_table(rows, ARENA_ROLES[label or "技术"])
        if not table:
            st.info("尚未运行真实模型竞技场")
        else:
            st.dataframe(table, hide_index=True, width="stretch")
            st.caption("排名第一仅作为推荐候选；切换生产模型需要人工确认。")
