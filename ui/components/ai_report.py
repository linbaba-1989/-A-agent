import streamlit as st

from ui.view_models import FUNDAMENTAL_GAP_MESSAGE, raw_json_expanded_default, report_view, safe_error


def _list(title: str, values) -> None:
    st.markdown(f"**{title}**")
    if isinstance(values, list) and values:
        for value in values:
            st.markdown(f"- {value}")
    else:
        st.caption("--")


def render_ai_report(result: dict) -> None:
    view = report_view(result)
    chief_row = result.get("chief_researcher", {})
    if not chief_row.get("success"):
        message, detail = safe_error(chief_row.get("error"))
        st.error(message)
        if detail:
            with st.expander("展开开发日志"):
                st.code(detail)
        return
    chief = view["chief"]
    st.subheader("AI 综合结论")
    cols = st.columns(5)
    for col, label, value in zip(cols, ("总体观点", "趋势状态", "风险等级", "置信度", "数据完整度"),
                                 (view["stance"], view["trend"], view["risk"],
                                  f"{view['confidence']} / 100" if view["confidence"] is not None else "--",
                                  view["data_status"])):
        col.metric(label, value or "--")
    if view["data_status"] != "可用":
        st.info(FUNDAMENTAL_GAP_MESSAGE + "；相关岗位会明确标注数据状态。")
    st.markdown("### 总研究员结论")
    st.write(chief.get("final_summary", "--"))
    employees = view["employees"]
    technical = (employees.get("technical_analyst", {}).get("data") or {})
    fundamental = (employees.get("fundamental_event_analyst", {}).get("data") or {})
    sentiment = (employees.get("sentiment_analyst", {}).get("data") or {})
    risk = (employees.get("risk_officer", {}).get("data") or {})
    tabs = st.tabs(["技术面", "基本面 / 事件", "市场情绪", "风险评估"])
    with tabs[0]:
        for label, key in (("趋势", "trend"), ("动量", "momentum"), ("量价", "volume_price"),
                           ("均线", "moving_average_structure"), ("支撑", "support"),
                           ("压力", "resistance"), ("突破", "breakout_status")):
            _list(label, technical.get(key) if isinstance(technical.get(key), list) else [technical.get(key)] if technical.get(key) else [])
    with tabs[1]:
        if fundamental.get("data_status") in {"unavailable", "partial"}:
            st.caption("数据：" + ("不可用" if fundamental.get("data_status") == "unavailable" else "部分可用"))
        st.write(fundamental.get("summary", "--"))
    with tabs[2]: st.write(sentiment.get("summary", "--"))
    with tabs[3]: st.write(risk.get("summary", "--"))
    left, right = st.columns(2)
    with left:
        _list("多头逻辑", chief.get("bull_case")); _list("关键催化剂", chief.get("key_catalysts"))
    with right:
        _list("空头逻辑", chief.get("bear_case")); _list("关键风险", chief.get("key_risks"))
    _list("数据缺口", chief.get("data_gaps"))
    st.caption("AI分析仅用于研究辅助，不构成投资建议。")
    with st.expander("查看原始分析数据", expanded=raw_json_expanded_default()):
        st.json(view["raw"])
