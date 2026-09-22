import html
from time import perf_counter

import streamlit as st

from src.market_clock import (DEFAULT_TRADING_CALENDAR, CLOSED, LUNCH_BREAK, OPEN, beijing_now,
                              market_session as current_market_session, should_fetch_quotes,
                              timestamp_to_beijing)
from src.realtime_market import (SnapshotConsumerState, market_quote_timestamp, rank_rows,
                                 refresh_interval_seconds)
from ui.components.market_status import render_status_strip
from ui.components.topbar import render_topbar_status
from ui.view_models import display_value, format_amount, format_number, quote_status_display


RANKINGS = {"涨幅榜": "change_pct", "跌幅榜": "change_pct_asc", "1分钟涨速": "speed_1m",
            "3分钟涨速": "speed_3m", "5分钟涨速": "speed_5m", "成交额": "amount",
            "换手率": "turnover_rate"}


def _table(rows: list[dict]) -> None:
    labels = ("代码", "名称", "最新", "涨幅%", "1m%", "3m%", "5m%", "换手%", "成交额", "最新更新时间")
    body = "<tr><td class='empty-row' colspan='10'>当前没有真实有效行情</td></tr>"
    if rows:
        rendered = []
        for row in rows:
            fields = (row.get("symbol"), row.get("name"), row.get("lastPrice"), row.get("change_pct"),
                      row.get("speed_1m"), row.get("speed_3m"), row.get("speed_5m"),
                      row.get("turnover_rate"), row.get("amount"), row.get("quote_time"))
            cells = []
            for index, value in enumerate(fields):
                shown = format_amount(value) if index == 8 else format_number(value) if 2 <= index <= 7 else display_value(value)
                css = row.get("flash_class", "") if index == 2 else ""
                cells.append(f"<td class='{css}'>{html.escape(shown)}</td>")
            rendered.append("<tr>" + "".join(cells) + "</tr>")
        body = "".join(rendered)
    head = "".join(f"<th>{label}</th>" for label in labels)
    st.markdown(f"<table class='terminal-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
                unsafe_allow_html=True)


def _events(previous: dict[str, dict], rows: list[dict]) -> list[str]:
    events = []
    for row in rows:
        old = previous.get(row["symbol"], {})
        name = row.get("name") or row["symbol"]
        for field, threshold, label in (("speed_1m", 1, "1分钟涨速"), ("amount", 1_000_000_000, "成交额突破10亿"),
                                        ("change_pct", 5, "涨幅突破5%")):
            try:
                current, before = float(row[field]), float(old[field])
            except (TypeError, ValueError):
                continue
            except KeyError:
                continue
            if current >= threshold and before < threshold:
                events.append(f"{row['symbol']} {name}　{label}" + (f" {current:+.2f}%" if field == "speed_1m" else ""))
    return events[:20]


def render(ctx: dict) -> None:
    st.title("实时行情")
    if st.toggle("动态模式 Beta", key="streaming_beta_enabled"):
        from ui.components.streaming_beta import render_streaming_beta
        render_streaming_beta(ctx)
        return
    st.caption("行情快照轮询 · 全A默认2秒 · 所有变化来自当前行情源")
    choice = st.segmented_control("排行榜", list(RANKINGS), default="涨幅榜", label_visibility="collapsed") or "涨幅榜"
    top_n = st.segmented_control("显示数量", [20, 50], default=20, format_func=lambda value: f"Top {value}",
                                 label_visibility="collapsed") or 20
    if ctx.get("realtime_feed") is None:
        st.info("行情：UNAVAILABLE；当前没有可用行情源。")
        return
    session = current_market_session(beijing_now(), ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
    market_open = session == OPEN
    enabled = bool(st.session_state.get("realtime_enabled", market_open))
    requested = int(st.session_state.get("realtime_frequency", 2))
    interval = refresh_interval_seconds(session, enabled, requested)

    @st.fragment(run_every=interval)
    def live_fragment():
        render_started = perf_counter()
        feed = ctx["realtime_feed"]
        now = beijing_now()
        session = current_market_session(now, ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
        previous_timestamp = st.session_state.get("realtime_quote_timestamp")
        enabled = bool(st.session_state.get("realtime_enabled", session == OPEN))
        if should_fetch_quotes(session) and enabled:
            # This is also safe when the parent app was rendered before a
            # session boundary: the probe is keyed by Beijing date/session.
            feed.ensure_fresh_provider_probe(session, now=now)
            rows = feed.snapshot(market_session=session, now=now)
        else:
            # Lunch/pre-open/closed may show the last real quote, but they do
            # not initiate a provider request.
            rows = feed.cached()
        current_timestamp = market_quote_timestamp(rows, feed.last_quote_timestamp)
        display_status = quote_status_display(ctx["status"], current_timestamp=current_timestamp,
                                              previous_timestamp=previous_timestamp, now=beijing_now(),
                                              market_session_value=session, feed=feed)
        ctx["status"].update(display_status)
        state = display_status["quote_status"]
        if current_timestamp is not None:
            st.session_state.realtime_quote_timestamp = current_timestamp
        st.session_state.realtime_quote_status = state
        st.session_state.realtime_market_session = session
        st.session_state.realtime_last_quote_time = display_status["last_quote_time"]
        slots = ctx.get("status_slots", {})
        if slots.get("topbar") is not None:
            render_topbar_status(slots["topbar"], ctx["status"])
        if slots.get("status_strip") is not None:
            render_status_strip(ctx["status"], target=slots["status_strip"])
        ranked = feed.enrich_static(rank_rows(rows, RANKINGS[choice], top_n))
        consumer = st.session_state.setdefault("realtime_market_consumer", SnapshotConsumerState())
        ranked = consumer.consume(ranked)
        if state != "LIVE":
            for row in ranked:
                row["flash_class"] = ""
        previous = st.session_state.get("realtime_previous_rows", {})
        events = _events(previous, rows) if rows else []
        st.session_state.realtime_previous_rows = {row["symbol"]: row for row in rows}
        badge = "live-badge" if state == "LIVE" else "stale-badge" if state == "STALE" else ""
        market_quote_time = timestamp_to_beijing(current_timestamp)
        market_quote_time_text = (market_quote_time.replace(tzinfo=None).isoformat(timespec="seconds")
                                  if market_quote_time else display_value(None))
        st.markdown(f"<div class='index-strip'><b>上证　--</b><b>深证　--</b><b>创业板　--</b>"
                    f"<span class='{badge}'>{state} ●</span><span>行情时间：{market_quote_time_text}</span></div>",
                    unsafe_allow_html=True)
        left, right = st.columns([4, 1], gap="small")
        with left: _table(ranked)
        with right:
            st.markdown("**行情异动**")
            if events:
                for event in events: st.caption(event)
            else: st.caption("暂无基于当前快照的事实异动")
        ui_latency = perf_counter() - render_started - feed.snapshot_latency
        if ctx.get("acceptance_mode", False):
            st.caption(f"行情快照 {feed.snapshot_latency:.3f}s｜界面 {max(0, ui_latency):.3f}s｜"
                       f"合计 {perf_counter()-render_started:.3f}s｜行情源初始化 {feed.provider_initializations}次｜"
                       f"请求 {feed.provider_request_count}｜锁跳过 {feed.skipped_due_to_lock}")

    live_fragment()
    if session == LUNCH_BREAK:
        st.info("市场午休，高频刷新已停止。")
    elif session == CLOSED:
        st.info("已收盘 / CACHED · 显示最近有效行情；1m/3m/5m 为最后有效值，无可靠历史时显示 --。")
    elif session != OPEN:
        st.info("当前不在连续竞价时段，高频刷新已停止。")
    elif not enabled:
        st.info("实时刷新已暂停。")
