import html
import streamlit as st


def render_status_strip(status: dict) -> None:
    connected = status["provider_status"] in ("connected", "fallback")
    provider_state = "已连接" if connected else "不可用"
    dot_class = "dot-ok" if connected else "dot-warn"
    st.markdown(
        "<div class='terminal-bar'>"
        f"{html.escape(str(status['provider']))} <span class='{dot_class}'>● {provider_state}</span>　｜　市场：{html.escape(str(status['market']))}　｜　"
        f"行情：{html.escape(str(status['last_quote_time']))}　｜　扫描池：{html.escape(str(status['active_universe']))}　｜　"
        f"刷新：--　｜　QMT备用：{'不可用' if 'unavailable' in str(status['qmt_fallback']) else '可用'}"
        "</div>", unsafe_allow_html=True)


def render_market_metrics(status: dict, elapsed=None) -> None:
    cols = st.columns(5)
    cols[0].metric("行情源", status["provider"])
    cols[1].metric("原始股票池", status["raw_universe"])
    cols[2].metric("可扫描股票", status["active_universe"])
    cols[3].metric("有效行情", status["valid_quotes"])
    cols[4].metric("缓存扫描耗时", f"{elapsed:.2f}s" if isinstance(elapsed, (int, float)) else "--")
