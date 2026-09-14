import html
from time import perf_counter

import streamlit as st

from src.realtime_market import SnapshotConsumerState, live_state, rank_rows, refresh_interval_seconds
from ui.view_models import display_value, format_amount, format_number


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
    st.caption("行情快照轮询 · 全A默认2秒 · 所有变化来自当前行情源")
    choice = st.segmented_control("排行榜", list(RANKINGS), default="涨幅榜", label_visibility="collapsed") or "涨幅榜"
    top_n = st.segmented_control("显示数量", [20, 50], default=20, format_func=lambda value: f"Top {value}",
                                 label_visibility="collapsed") or 20
    market_open = ctx["status"].get("market") == "交易中"
    enabled = bool(st.session_state.get("realtime_enabled", market_open))
    requested = int(st.session_state.get("realtime_frequency", 2))
    interval = refresh_interval_seconds(market_open, enabled, requested)

    @st.fragment(run_every=interval)
    def live_fragment():
        render_started = perf_counter()
        feed = ctx["realtime_feed"]
        previous_timestamp = st.session_state.get("realtime_quote_timestamp")
        rows = feed.snapshot()
        if rows:
            current_timestamp = max(row["quote_timestamp"] for row in rows)
            st.session_state.realtime_quote_timestamp = current_timestamp
            state = live_state(previous_timestamp, current_timestamp) if market_open else "CLOSED"
            st.session_state.realtime_live_state = state
            ranked = feed.enrich_static(rank_rows(rows, RANKINGS[choice], top_n))
            consumer = st.session_state.setdefault("realtime_market_consumer", SnapshotConsumerState())
            ranked = consumer.consume(ranked)
            previous = st.session_state.get("realtime_previous_rows", {})
            events = _events(previous, rows)
            st.session_state.realtime_previous_rows = {row["symbol"]: row for row in rows}
        else:
            current_timestamp = getattr(feed, "last_quote_timestamp", None)
            state = "CLOSED" if not market_open else "STALE"
            st.session_state.realtime_live_state = state
            ranked, events = [], []
        badge = "live-badge" if state == "LIVE" else "stale-badge" if state == "STALE" else ""
        st.markdown(f"<div class='index-strip'><b>上证　--</b><b>深证　--</b><b>创业板　--</b>"
                    f"<span class='{badge}'>{state} ●</span><span>行情时间：{display_value(ranked[0].get('quote_time') if ranked else None)}</span></div>",
                    unsafe_allow_html=True)
        left, right = st.columns([4, 1], gap="small")
        with left: _table(ranked)
        with right:
            st.markdown("**行情异动**")
            if events:
                for event in events: st.caption(event)
            else: st.caption("暂无基于当前快照的事实异动")
        ui_latency = perf_counter() - render_started - feed.snapshot_latency
        st.caption(f"行情快照 {feed.snapshot_latency:.3f}s｜界面 {max(0, ui_latency):.3f}s｜"
                   f"合计 {perf_counter()-render_started:.3f}s｜行情源初始化 {feed.provider_initializations}次｜"
                   f"请求 {feed.provider_request_count}｜锁跳过 {feed.skipped_due_to_lock}")

    live_fragment()
    if not market_open:
        st.info("市场已收盘，高频刷新已停止。")
    elif not enabled:
        st.info("实时刷新已暂停。")
