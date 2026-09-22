from concurrent.futures import ThreadPoolExecutor
import time

import streamlit as st

from src.workforce_acceptance import build_fact_bundle
from ui.components.ai_progress import render_ai_states
from ui.components.ai_report import render_ai_report
from ui.components.kline_chart import render_kline
from ui.components.stock_header import render_stock_header
from ui.components.technical_panel import render_technical_panel
from ui.stock_research_service import StockResearchService, toggle_watchlist
from ui.components.market_status import render_status_strip
from ui.components.topbar import render_topbar_status
from ui.view_models import (ANALYSIS_MODE_LABELS, FUNDAMENTAL_GAP_MESSAGE, analysis_mode_display,
                            analysis_mode_value, normalize_symbol, quote_status_display, safe_error)
from src.market_clock import (DEFAULT_TRADING_CALENDAR, OPEN, beijing_now,
                              market_session as current_market_session, should_fetch_quotes)
from src.realtime_market import SnapshotConsumerState, market_quote_timestamp, refresh_interval_seconds
from ui.research_view import configured_models


def _render_live_quote(ctx: dict, symbol: str, initial_quote: dict, name: str) -> None:
    session = current_market_session(beijing_now(), ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
    market_open = session == OPEN
    interval = refresh_interval_seconds(session, st.session_state.get("realtime_enabled", market_open), 1)

    @st.fragment(run_every=interval)
    def quote_fragment():
        now = beijing_now()
        session = current_market_session(now, ctx.get("market_calendar", DEFAULT_TRADING_CALENDAR))
        enabled = bool(st.session_state.get("realtime_enabled", session == OPEN))
        feed = ctx["realtime_feed"]
        if should_fetch_quotes(session) and enabled:
            feed.ensure_fresh_provider_probe(session, now=now, symbols=[symbol])
            rows = feed.snapshot([symbol], market_session=session, now=now)
        else:
            rows = feed.cached([symbol])
        previous_market_timestamp = st.session_state.get("realtime_quote_timestamp")
        current_market_timestamp = market_quote_timestamp(rows, feed.last_quote_timestamp)
        display_status = quote_status_display(
            ctx["status"], current_timestamp=current_market_timestamp,
            previous_timestamp=previous_market_timestamp, now=beijing_now(),
            market_session_value=session, feed=feed)
        ctx["status"].update(display_status)
        if current_market_timestamp is not None:
            st.session_state.realtime_quote_timestamp = current_market_timestamp
        st.session_state.realtime_quote_status = display_status["quote_status"]
        st.session_state.realtime_market_session = session
        st.session_state.realtime_last_quote_time = display_status["last_quote_time"]
        slots = ctx.get("status_slots", {})
        if slots.get("topbar") is not None:
            render_topbar_status(slots["topbar"], ctx["status"])
        if slots.get("status_strip") is not None:
            render_status_strip(ctx["status"], target=slots["status_strip"])
        if rows:
            consumer = st.session_state.setdefault(f"stock_snapshot_consumer_{symbol}", SnapshotConsumerState())
            row = consumer.consume(rows)[0]
            quote_time = row.get("quote_time", "unavailable")
            quote = {**initial_quote, **row, "timestamp": row["quote_time"]}
            render_stock_header(symbol, quote, name, display_status["market"])
            cols = st.columns(3)
            cols[0].metric("1分钟涨速", "--" if row["speed_1m"] == "unavailable" else f"{row['speed_1m']:.2f}%")
            cols[1].metric("3分钟涨速", "--" if row["speed_3m"] == "unavailable" else f"{row['speed_3m']:.2f}%")
            cols[2].metric("5分钟涨速", "--" if row["speed_5m"] == "unavailable" else f"{row['speed_5m']:.2f}%")
        else:
            render_stock_header(symbol, initial_quote, name, display_status["market"])

    quote_fragment()


def _run_research(ctx: dict, symbol: str, mode: str, facts: dict) -> dict:
    placeholder = st.empty()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ctx["workforce"].analyze, symbol, facts, mode)
        while not future.done():
            with placeholder.container():
                st.warning("AI研究正在运行，请勿重复提交")
                render_ai_states(ctx["workforce"].router.role_states)
            time.sleep(.5)
        result = future.result()
    result["elapsed_seconds"] = time.perf_counter() - started
    result["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    placeholder.empty()
    return result


def _research_facts(ctx: dict, symbol: str) -> dict:
    facts = build_fact_bundle(symbol, provider=ctx["provider"], scanner=ctx["scanner"])
    # Current market state is UI context, not inferred from a closing tick time.
    session = ctx["status"].get("market_session", current_market_session(beijing_now()))
    return {**facts, "market_status": session,
            "quote_type": "realtime_snapshot" if should_fetch_quotes(session) else "latest_available_snapshot",
            "source": ctx["status"].get("provider", facts.get("source"))}


def _recent_records(symbol: str) -> list[dict]:
    return [row for row in st.session_state.get("research_history", []) if row.get("symbol") == symbol]


def _render_records(symbol: str) -> None:
    st.subheader("最近AI研究")
    records = _recent_records(symbol)
    if not records:
        st.info("暂无研究记录")
        return
    rows = []
    for result in records:
        chief = result.get("chief_researcher", {})
        data = chief.get("data") or {}
        bull, bear = len(data.get("bull_case", [])), len(data.get("bear_case", []))
        rows.append({"时间": result.get("created_at", "--"), "模式": analysis_mode_display(result.get("analysis_mode")),
                     "观点": "偏多" if bull > bear else "偏空" if bear > bull else "中性",
                     "置信度": data.get("confidence", "--"),
                     "耗时": f"{result.get('elapsed_seconds', 0):.2f}s" if result.get("elapsed_seconds") else "--",
                     "状态": "完成" if chief.get("success") else "失败"})
    st.dataframe(rows, hide_index=True, width="stretch")
    selected = st.selectbox("展开历史报告", range(len(records)),
                            format_func=lambda index: f"{rows[index]['时间']}｜{rows[index]['模式']}｜{rows[index]['状态']}")
    with st.expander("查看报告"):
        render_ai_report(records[selected])


def render(ctx: dict, symbol: str, mode: str) -> None:
    st.title("个股研究")
    symbol = normalize_symbol(symbol or st.session_state.get("selected_symbol", "600498.SH"))
    if not ctx["available"]:
        st.error("行情服务不可用")
        return
    service = StockResearchService(ctx["provider"], ctx["scanner"], ctx["status"])
    try:
        snapshot = service.load(symbol)
    except LookupError:
        st.warning("证券不存在或当前行情源无该证券")
        return
    except Exception as exc:
        message, detail = safe_error(str(exc))
        st.error("当前无有效行情" if "quote" in str(exc).lower() else message)
        if detail:
            with st.expander("查看详细错误"): st.code(detail)
        return

    watchlist = st.session_state.setdefault("watchlist", ["600498.SH"])
    heading, action = st.columns([7, 1])
    heading.caption(f"基础行情加载 {snapshot.load_seconds:.2f}s｜Provider 实例复用")
    watched = symbol in watchlist
    if action.button("★ 已自选" if watched else "☆ 加入自选", width="stretch"):
        toggle_watchlist(watchlist, symbol); st.rerun()
    _render_live_quote(ctx, symbol, snapshot.quote, snapshot.facts["name"])

    chart_area, technical = st.columns([3.25, 1], gap="medium")
    with chart_area:
        chart_type = st.segmented_control("周期", ["分时", "日K", "周K", "月K"], default="日K",
                                          label_visibility="collapsed") or "日K"
        if chart_type == "分时":
            points = ctx["realtime_feed"].buffer.points(symbol)
            if len(points) >= 2:
                st.line_chart({"最新价": [point.last_price for point in points]}, height=380)
            else:
                st.info("分时从本次连接开始记录；当前快照不足，不补造连接前行情。")
        elif chart_type in ("周K", "月K"):
            st.info(f"{chart_type} 后续支持；不会使用日K数据冒充。")
        else:
            days = st.segmented_control("时间区间", [20, 60, 120, 250], default=120,
                                        format_func=lambda value: f"{value}日") or 120
            last_date = snapshot.history["date"].max()
            st.caption(f"日K：更新至 {last_date:%Y-%m-%d}｜已加载 {len(snapshot.history)} 条")
            render_kline(snapshot.history, days)
    with technical:
        render_technical_panel(snapshot.facts)

    st.subheader("AI研究")
    st.caption(f"市场状态：{ctx['status'].get('market', '--')}｜行情时间：{snapshot.quote.get('timestamp', '--')}"
               "｜使用最近有效行情与历史数据")
    selected_mode = st.segmented_control("AI研究模式", ["标准", "深度", "MAX"],
                                         default=analysis_mode_display(mode) if mode in ANALYSIS_MODE_LABELS else "标准",
                                         label_visibility="collapsed") or "标准"
    ai_mode = analysis_mode_value(selected_mode)
    render_ai_states({role: "idle" for role in ctx["routes"]}, facts=snapshot.facts,
                     models=configured_models(ctx))
    st.caption(FUNDAMENTAL_GAP_MESSAGE + "；相关岗位会明确记录数据缺口。")
    if st.button("开始AI研究", type="primary"):
        try:
            facts = _research_facts(ctx, symbol)
            result = _run_research(ctx, symbol, ai_mode, facts)
            st.session_state.last_research = result
            st.session_state.setdefault("research_history", []).insert(0, result)
        except Exception as exc:
            message, detail = safe_error(str(exc)); st.error("AI研究失败")
            with st.expander("查看详细错误"): st.code(detail or message)
    latest = next(iter(_recent_records(symbol)), None)
    if latest:
        render_ai_states(ctx["workforce"].router.role_states,
                         {**latest.get("employees", {}), "chief_researcher": latest.get("chief_researcher", {})},
                         facts=latest.get("fact_data", snapshot.facts), models=configured_models(ctx))
    _render_records(symbol)
