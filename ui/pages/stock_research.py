from concurrent.futures import ThreadPoolExecutor
import time

import pandas as pd
import streamlit as st

from ui.components.ai_progress import render_ai_states
from ui.components.ai_report import render_ai_report
from ui.components.stock_header import render_stock_header
from ui.view_models import display_value, safe_error
from src.workforce_acceptance import build_fact_bundle


def _chart(history: pd.DataFrame) -> None:
    if history is None or history.empty:
        st.info("历史K线 unavailable")
        return
    try:
        import plotly.graph_objects as go
        x = history["time"] if "time" in history else history.index
        fig = go.Figure(go.Candlestick(x=x, open=history["open"], high=history["high"],
                                       low=history["low"], close=history["close"], name="日K",
                                       increasing_line_color="#d92d20", decreasing_line_color="#079455"))
        for window, color in ((5, "#f79009"), (10, "#175cd3"), (20, "#7f56d9"), (60, "#344054")):
            if len(history) >= window:
                fig.add_trace(go.Scatter(x=x, y=pd.to_numeric(history["close"]).rolling(window).mean(),
                                         name=f"MA{window}", line={"width": 1, "color": color}))
        fig.update_layout(height=480, margin=dict(l=10, r=10, t=20, b=10), xaxis_rangeslider_visible=False,
                          paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", legend_orientation="h")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    except (ImportError, KeyError):
        st.line_chart(history["close"], height=420)


def _run_research(ctx: dict, symbol: str, mode: str, facts: dict) -> dict:
    placeholder = st.empty()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ctx["workforce"].analyze, symbol, facts, mode)
        while not future.done():
            with placeholder.container():
                st.caption("真实任务状态；无模拟进度百分比")
                render_ai_states(ctx["workforce"].router.role_states)
            time.sleep(0.5)
        result = future.result()
    placeholder.empty()
    render_ai_states(ctx["workforce"].router.role_states,
                     {**result["employees"], "chief_researcher": result["chief_researcher"]})
    return result


def render(ctx: dict, symbol: str, mode: str) -> None:
    st.title("个股研究")
    symbol = symbol or "600498.SH"
    if not ctx["available"]:
        st.error("行情服务不可用")
        return
    quote = ctx["provider"].get_quote(symbol)
    if not quote:
        st.warning("没有获得有效行情。")
        return
    render_stock_header(symbol, quote, quote.get("name", ""))
    tabs = st.tabs(["分时", "日K", "周K", "月K"])
    with tabs[0]: st.info("分时图将在交易时段使用实时快照；当前不使用旧缓存伪造分钟行情。")
    history = ctx["provider"].get_history([symbol], "1d", 90).get(symbol)
    with tabs[1]: _chart(history)
    with tabs[2]: st.info("周K unavailable")
    with tabs[3]: st.info("月K unavailable")
    try:
        facts = build_fact_bundle(symbol, provider=ctx["provider"], scanner=ctx["scanner"])
    except Exception as exc:
        message, detail = safe_error(str(exc)); st.error(message)
        with st.expander("展开开发日志"): st.code(detail)
        return
    cols = st.columns(9)
    for col, key, label in zip(cols, ("ma5", "ma10", "ma20", "ma60", "atr14", "turnover_rate", "speed_1m", "speed_3m", "speed_5m"),
                               ("MA5", "MA10", "MA20", "MA60", "ATR", "换手率", "1m", "3m", "5m")):
        value = facts.get(key)
        if key.startswith("speed_") and facts.get("market_status") != "open": value = "unavailable"
        col.metric(label, display_value(value))
    if facts.get("market_status") != "open": st.caption("分钟涨速仅交易时段有效")
    st.subheader("AI研究")
    st.caption(f"模式：{mode.upper()}｜技术、基本面/事件、市场情绪、风险并行；满足条件后总研究员综合。")
    if st.button("启动 AI 研究", type="primary"):
        result = _run_research(ctx, symbol, mode, facts)
        st.session_state.last_research = result
        history_rows = st.session_state.setdefault("research_history", [])
        history_rows.insert(0, result)
    if st.session_state.get("last_research", {}).get("symbol") == symbol:
        render_ai_report(st.session_state.last_research)
