import streamlit as st

from ui.components.stock_table import render_stock_table
from ui.view_models import normalize_symbol


def render(ctx: dict) -> None:
    st.title("自选股")
    watchlist = st.session_state.setdefault("watchlist", ["600498.SH"])
    left, right = st.columns([4, 1])
    candidate = normalize_symbol(left.text_input("添加代码", placeholder="600498 / 600498.SH"))
    if right.button("添加", width="stretch") and candidate and candidate not in watchlist:
        watchlist.append(candidate); st.rerun()
    ticks = ctx["provider"].get_full_ticks(watchlist) if ctx["available"] and watchlist else {}
    rows = [{"symbol": code, "name": "", "last_price": tick.get("lastPrice"),
             "change_pct": ((tick.get("lastPrice", 0) / tick.get("lastClose", 1) - 1) * 100)
             if tick.get("lastClose") else "unavailable", "amount": tick.get("amount")}
            for code, tick in ticks.items()]
    render_stock_table(rows, "watchlist")
    if watchlist:
        remove = st.selectbox("移除自选", watchlist)
        if st.button("删除"):
            watchlist.remove(remove); st.rerun()
