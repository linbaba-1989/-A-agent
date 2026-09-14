import streamlit as st
import pandas as pd

from src.realtime_market import SnapshotConsumerState, refresh_interval_seconds
from ui.view_models import format_amount, format_number
from ui.view_models import normalize_symbol


def render(ctx: dict) -> None:
    st.title("自选股")
    watchlist = st.session_state.setdefault("watchlist", ["600498.SH"])
    left, right = st.columns([4, 1])
    candidate = normalize_symbol(left.text_input("添加代码", placeholder="600498 / 600498.SH"))
    if right.button("添加", width="stretch") and candidate and candidate not in watchlist:
        watchlist.append(candidate); st.rerun()
    market_open = ctx["status"].get("market") == "交易中"
    interval = refresh_interval_seconds(market_open, st.session_state.get("realtime_enabled", market_open), 1)

    @st.fragment(run_every=interval)
    def watchlist_fragment():
        rows = ctx["realtime_feed"].snapshot(watchlist) if ctx["available"] and watchlist else []
        rows = ctx["realtime_feed"].enrich_static(rows)
        consumer = st.session_state.setdefault("watchlist_snapshot_consumer", SnapshotConsumerState())
        rows = consumer.consume(rows)
        if not rows:
            st.info("当前没有真实自选行情")
            return
        frame = pd.DataFrame([{"代码": row["symbol"], "名称": row.get("name") or "--",
                               "最新": format_number(row["lastPrice"]), "涨幅%": format_number(row["change_pct"]),
                               "1m%": format_number(row["speed_1m"]), "3m%": format_number(row["speed_3m"]),
                               "5m%": format_number(row["speed_5m"]), "换手%": format_number(row["turnover_rate"]),
                               "成交额": format_amount(row["amount"]),
                               "走势": ctx["realtime_feed"].buffer.prices(row["symbol"], 120)} for row in rows])
        st.dataframe(frame, hide_index=True, width="stretch",
                     column_config={"走势": st.column_config.LineChartColumn("会话走势", width="medium")})
        st.caption("Sparkline仅记录本次连接后的真实snapshot，最多保留120点。")

    watchlist_fragment()
    if watchlist:
        remove = st.selectbox("移除自选", watchlist)
        if st.button("删除"):
            watchlist.remove(remove); st.rerun()
