import streamlit as st

from ui.view_models import normalize_symbol


def render_topbar(status: dict) -> tuple[str, str]:
    brand, search, mode, market = st.columns([1.05, 2.5, 1.7, 2.25], vertical_alignment="center")
    brand.markdown("**A-Agent** <span style='color:#667085;font-size:11px'>Quant Terminal</span>", unsafe_allow_html=True)
    query = search.text_input("全局股票搜索", st.session_state.get("selected_symbol", "600498.SH"),
                              label_visibility="collapsed", placeholder="代码 / 名称（当前支持代码）")
    analysis_mode = mode.segmented_control("研究模式", ["标准", "深度", "MAX"], default="标准",
                                           label_visibility="collapsed") or "标准"
    dot = "<span class='dot-ok'>●</span>" if status["provider_status"] == "connected" else "<span class='dot-warn'>●</span>"
    market.markdown(f"<span style='font-size:12px'><b>{status['provider']}</b> {dot}　{status['market']}　{status['last_quote_time']}</span>", unsafe_allow_html=True)
    symbol = normalize_symbol(query)
    if symbol:
        st.session_state.selected_symbol = symbol
    return symbol, {"标准": "standard", "深度": "deep", "MAX": "max"}[analysis_mode]
