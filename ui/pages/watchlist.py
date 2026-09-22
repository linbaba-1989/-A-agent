import streamlit as st
import pandas as pd

from src.market_clock import (DEFAULT_TRADING_CALENDAR, OPEN, beijing_now,
                              market_session as current_market_session, should_fetch_quotes)
from src.realtime_market import SnapshotConsumerState, market_quote_timestamp, refresh_interval_seconds
from ui.components.market_status import render_status_strip
from ui.components.topbar import render_topbar_status
from ui.view_models import format_amount, format_number, quote_status_display
from ui.view_models import normalize_symbol


def render(ctx: dict) -> None:
    st.title("自选股")
    watchlist = st.session_state.setdefault("watchlist", ["600498.SH"])
    left, right = st.columns([4, 1])
    candidate = normalize_symbol(left.text_input("添加代码", placeholder="600498 / 600498.SH"))
    if right.button("添加", width="stretch") and candidate and candidate not in watchlist:
        watchlist.append(candidate); st.rerun()
    feed = ctx.get("realtime_feed")
    if feed is None:
        st.info("行情：UNAVAILABLE；当前没有可用行情源。")
        return
    session = current_market_session(beijing_now(), ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
    market_open = session == OPEN
    interval = refresh_interval_seconds(session, st.session_state.get("realtime_enabled", market_open), 1)

    @st.fragment(run_every=interval)
    def watchlist_fragment():
        now = beijing_now()
        session = current_market_session(now, ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
        enabled = bool(st.session_state.get("realtime_enabled", session == OPEN))
        if ctx["available"] and watchlist and should_fetch_quotes(session) and enabled:
            feed.ensure_fresh_provider_probe(session, now=now, symbols=watchlist)
            rows = feed.snapshot(watchlist, market_session=session, now=now)
        elif ctx["available"] and watchlist:
            rows = feed.cached(watchlist)
        else:
            rows = []
        previous_timestamp = st.session_state.get("realtime_quote_timestamp")
        current_timestamp = market_quote_timestamp(rows, feed.last_quote_timestamp)
        display_status = quote_status_display(ctx["status"], current_timestamp=current_timestamp,
                                              previous_timestamp=previous_timestamp, now=beijing_now(),
                                              market_session_value=session, feed=feed)
        ctx["status"].update(display_status)
        if current_timestamp is not None:
            st.session_state.realtime_quote_timestamp = current_timestamp
        st.session_state.realtime_quote_status = display_status["quote_status"]
        st.session_state.realtime_market_session = session
        st.session_state.realtime_last_quote_time = display_status["last_quote_time"]
        slots = ctx.get("status_slots", {})
        if slots.get("topbar") is not None:
            render_topbar_status(slots["topbar"], ctx["status"])
        if slots.get("status_strip") is not None:
            render_status_strip(ctx["status"], target=slots["status_strip"])
        rows = feed.enrich_static(rows)
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
                               "走势": feed.buffer.prices(row["symbol"], 120)} for row in rows])
        st.dataframe(frame, hide_index=True, width="stretch",
                     column_config={"走势": st.column_config.LineChartColumn("会话走势", width="medium")})
        st.caption("Sparkline仅记录本次连接后的真实snapshot，最多保留120点。")

    watchlist_fragment()
    if watchlist:
        remove = st.selectbox("移除自选", watchlist)
        if st.button("删除"):
            watchlist.remove(remove); st.rerun()
