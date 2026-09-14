import streamlit as st

from ui.view_models import ANALYSIS_MODE_LABELS, analysis_mode_value, normalize_symbol


def render_topbar(status: dict) -> tuple[str, str]:
    brand, search, mode, live, frequency, market = st.columns([1.05, 2.2, 1.45, .9, 1.05, 2.1], vertical_alignment="center")
    brand.markdown("**A-Agent** <span style='color:#667085;font-size:11px'>Quant Terminal</span>", unsafe_allow_html=True)
    query = search.text_input("全局股票搜索", st.session_state.get("selected_symbol", "600498.SH"),
                              label_visibility="collapsed", placeholder="代码 / 名称（当前支持代码）")
    analysis_mode = mode.segmented_control("研究模式", list(ANALYSIS_MODE_LABELS.values()), default="标准",
                                           label_visibility="collapsed") or "标准"
    market_open = status.get("market") == "交易中"
    if "realtime_enabled" not in st.session_state:
        st.session_state.realtime_enabled = market_open
    live.toggle("实时刷新 ●", key="realtime_enabled")
    frequency.segmented_control("刷新频率", [1, 2, 5], default=2, format_func=lambda value: f"{value}秒",
                                key="realtime_frequency", label_visibility="collapsed")
    dot = "<span class='dot-ok'>●</span>" if status["provider_status"] == "connected" else "<span class='dot-warn'>●</span>"
    market.markdown(f"<span style='font-size:12px'><b>{status['provider']}</b> {dot}　{status['market']}　{status['last_quote_time']}</span>", unsafe_allow_html=True)
    symbol = normalize_symbol(query)
    if symbol:
        st.session_state.selected_symbol = symbol
    return symbol, analysis_mode_value(analysis_mode)
