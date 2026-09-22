import html
from typing import Any

import streamlit as st

from ui.view_models import quote_status_display


def render_status_strip(status: dict, target: Any | None = None) -> None:
    target = target or st
    connected = status["provider_status"] in ("connected", "fallback")
    provider_state = "已连接" if connected else "不可用"
    dot_class = "dot-ok" if connected else "dot-warn"
    display_status = quote_status_display(status)
    realtime_state = display_status["quote_status"]
    last_quote_time = display_status.get("last_quote_time", status.get("last_quote_time", "unavailable"))
    last_quote_display = str(last_quote_time).replace("T", " ")
    target.markdown(
        "<div class='terminal-bar'>"
        f"{html.escape(str(status['provider']))} <span class='{dot_class}'>● {provider_state}</span>　｜　市场：{html.escape(str(display_status['market']))}　｜　"
        f"行情：{html.escape(str(realtime_state))}　｜　最后行情：{html.escape(last_quote_display)}　｜　"
        f"扫描池：{html.escape(str(status['active_universe']))}　｜　"
        f"刷新：{html.escape(realtime_state)}　｜　QMT备用：{'不可用' if 'unavailable' in str(status['qmt_fallback']) else '可用'}"
        "</div>", unsafe_allow_html=True)


def render_market_metrics(status: dict, elapsed=None) -> None:
    cols = st.columns(5)
    cols[0].metric("行情源", status["provider"])
    cols[1].metric("原始股票池", status["raw_universe"])
    cols[2].metric("可扫描股票", status["active_universe"])
    cols[3].metric("有效行情", status["valid_quotes"])
    cols[4].metric("缓存扫描耗时", f"{elapsed:.2f}s" if isinstance(elapsed, (int, float)) else "--")
