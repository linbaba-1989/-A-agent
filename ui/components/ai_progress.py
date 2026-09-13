import streamlit as st

from ui.view_models import role_state


ROLE_NAMES = {"technical_analyst": "技术分析员 · DeepSeek V4-Pro",
              "fundamental_event_analyst": "基本面/事件分析员 · Qwen 3.8 Max",
              "sentiment_analyst": "市场情绪分析员 · Doubao",
              "risk_officer": "风险官 · Kimi K3", "chief_researcher": "总研究员 · DeepSeek V4-Pro"}


def render_ai_states(states: dict, results: dict | None = None) -> None:
    results = results or {}
    cols = st.columns(5)
    for col, role in zip(cols, ROLE_NAMES):
        result = results.get(role, {})
        status = result.get("status") or ("complete" if result.get("success") else states.get(role))
        col.markdown(f"**{ROLE_NAMES[role]}**")
        col.caption(f"状态：{role_state(status)}")
        if result.get("latency") is not None:
            col.caption(f"耗时：{result['latency']:.2f}s")
