import streamlit as st

from ui.components.ai_report import render_ai_report


def render(ctx: dict) -> None:
    st.title("AI研究院")
    st.caption("最近 AI 研究记录；模型配置位于设置。")
    records = st.session_state.get("research_history", [])
    if not records:
        st.info("暂无研究记录。请从个股研究页启动。")
        return
    options = []
    for index, row in enumerate(records):
        chief = row.get("chief_researcher", {})
        confidence = (chief.get("data") or {}).get("confidence", "--")
        options.append(f"{row.get('symbol')}｜{row.get('analysis_mode')}｜置信度 {confidence}｜{'完成' if chief.get('success') else '失败'}")
    selected = st.selectbox("研究记录", range(len(options)), format_func=lambda index: options[index])
    render_ai_report(records[selected])
