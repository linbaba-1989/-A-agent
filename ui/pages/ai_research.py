import streamlit as st

from ui.components.ai_report import render_ai_report
from ui.view_models import analysis_mode_display
from ui.research_view import model_display

ARENA_ROLES = {"技术": "technical_analyst", "基本面": "fundamental_event_analyst",
               "情绪": "sentiment_analyst", "风险": "risk_officer", "总研究员": "chief_researcher"}


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
    st.caption("最近 AI 研究记录；模型配置位于设置。")
    history_tab, arena_tab = st.tabs(["研究记录", "模型竞技场"])
    with history_tab:
        records = st.session_state.get("research_history", [])
        if not records:
            st.info("暂无研究记录。请从个股研究页启动。")
        else:
            options = []
            for index, row in enumerate(records):
                chief = row.get("chief_researcher", {})
                confidence = (chief.get("data") or {}).get("confidence", "--")
                options.append(f"{row.get('symbol')}｜{analysis_mode_display(row.get('analysis_mode'))}｜置信度 {confidence}｜{'完成' if chief.get('success') else '失败'}")
            selected = st.selectbox("研究记录", range(len(options)), format_func=lambda index: options[index])
            render_ai_report(records[selected])
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
