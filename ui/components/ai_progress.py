import html
import streamlit as st
from ui.research_view import ROLES, DATA_LABELS, elapsed_display, execution_state, model_display, workforce_data

ROLE_NAMES = ROLES


def render_ai_states(states, results=None, *, facts=None, models=None):
    results, models = results or {}, models or {}
    data = workforce_data({"fact_data": facts, "employees": results,
                           "chief_researcher": results.get("chief_researcher", {})})
    colors = {"等待": "#667085", "运行中": "#175cd3", "完成": "#067647", "失败": "#b54708", "超时": "#b54708"}
    for col, (role, name) in zip(st.columns(5), ROLES.items()):
        row = results.get(role) or {}
        status = execution_state(states.get(role), row)
        model = model_display(row.get("actual_model") or row.get("model") or models.get(role))
        if row.get("fallback") is True: model += "（fallback）"
        with col.container(border=True):
            st.markdown(f"**{name}**")
            st.caption(model)
            st.markdown(f"<span style='color:{colors[status]}'>状态：{html.escape(status)}</span>", unsafe_allow_html=True)
            st.caption(f"耗时：{elapsed_display(row.get('latency'))}")
            st.caption(f"数据：{DATA_LABELS[data[role]]}")
            if row.get("success") and data[role] != "available":
                st.caption("基于有限数据生成")
