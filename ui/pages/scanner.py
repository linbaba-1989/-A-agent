import streamlit as st

from ui.components.stock_table import render_stock_table
from ui.view_models import RANGE_PLACEHOLDERS, filter_scan_rows, history_status_display
from ui.stock_research_service import select_research_symbol


def _range(container, label, prefix, suffix=""):
    columns = container.columns([1.15, .9, .18, .9, .25])
    columns[0].caption(label)
    low = columns[1].number_input(label + "最低", value=None, placeholder=RANGE_PLACEHOLDERS[0],
                                  format="%.2f", label_visibility="collapsed", key=prefix + "_min")
    columns[2].markdown("～")
    high = columns[3].number_input(label + "最高", value=None, placeholder=RANGE_PLACEHOLDERS[1],
                                   format="%.2f", label_visibility="collapsed", key=prefix + "_max")
    if suffix: columns[4].caption(suffix)
    return low, high


def render(ctx: dict) -> None:
    st.title("全A扫描")
    if ctx["status"].get("market_session") == "closed":
        st.info("盘后模式 / 使用最近有效收盘快照")
        st.caption("涨速使用最后有效值；没有可靠历史时显示 --。技术指标与筛选条件仍可使用。")
    with st.container(border=True):
        first = st.columns(3)
        price_min, price_max = _range(first[0], "价格", "price")
        change_min, change_max = _range(first[1], "涨跌幅", "change", "%")
        turnover_min, turnover_max = _range(first[2], "换手率", "turnover", "%")

        second = st.columns(4)
        amount_group = second[0].columns([1.1, 1])
        amount_group[0].caption("成交额 ≥")
        amount_yi = amount_group[1].number_input("成交额亿元", value=None, placeholder="亿元", format="%.2f",
                                                 label_visibility="collapsed")
        speed = {}
        for container, minute in zip(second[1:], (1, 3, 5)):
            group = container.columns([1.1, 1, .2])
            group[0].caption(f"{minute}m涨速 ≥")
            speed[minute] = group[1].number_input(f"{minute}m涨速", value=None, placeholder="%",
                                                  format="%.2f", label_visibility="collapsed")
            group[2].caption("%")

        third = st.columns([2.2, 4.8, 1.6])
        market = third[0].segmented_control("市场", ["全部", "沪市", "深市", "北交所"], default="全部",
                                            label_visibility="collapsed")
        options = third[1].columns(6)
        above = {window: options[index].checkbox(f"MA{window}") for index, window in enumerate((5, 10, 20, 60))}
        recent_high = options[4].checkbox("近期新高")
        ma_breakout = options[5].checkbox("MA突破")
        actions = third[2].columns([.8, 1.2])
        reset = actions[0].button("重置", width="stretch")
        start = actions[1].button("开始扫描", type="primary", disabled=not ctx["available"], width="stretch")
        if reset:
            for key in tuple(st.session_state):
                if key not in ("selected_symbol", "scan_rows", "research_history",
                               "market_source", "acceptance_mode", "acceptance_source"):
                    del st.session_state[key]
            st.rerun()

    if start:
        progress = st.progress(0, text="正在初始化历史指标……")
        result = ctx["scanner"].scan(100, progress_callback=lambda value, msg: progress.progress(min(float(value), 1.0), text=msg))
        progress.empty()
        st.session_state.scan_rows = result.rows
        st.session_state.last_scan_elapsed = result.diagnostics.elapsed_seconds

    filters = {"market": market, "price_min": price_min, "price_max": price_max,
               "change_min": change_min, "change_max": change_max,
               "turnover_min": turnover_min, "turnover_max": turnover_max,
               "amount_min": amount_yi * 100_000_000 if amount_yi is not None else None,
               "speed_1m": speed[1], "speed_3m": speed[3], "speed_5m": speed[5],
               "recent_high": recent_high, "ma_breakout": ma_breakout,
               **{f"above_ma{window}": enabled for window, enabled in above.items()}}
    if ctx.get("scanner") is None:
        st.info("请在设置中连接行情源后运行扫描；收盘不限制扫描功能。")
        return
    history = ctx["scanner"].history_service.info()
    technical_filter_requested = recent_high or ma_breakout or any(above.values())
    if technical_filter_requested and history.status in {"not_started", "initializing", "failed"}:
        st.warning("技术指标缓存尚未就绪；本次暂不应用均线、新高和突破筛选。")
        filters.update({f"above_ma{window}": False for window in (5, 10, 20, 60)})
        filters.update({"recent_high": False, "ma_breakout": False})
    source_rows = st.session_state.get("scan_rows", [])
    rows = filter_scan_rows(source_rows, filters)
    st.caption(f"历史指标：{history_status_display(history.status)}｜可用 {history.ready}/{history.total}"
               f"｜不足60根 {history.insufficient}｜无历史 {history.unavailable}｜失败 {history.failed}")
    st.caption(f"扫描返回：{len(source_rows)}｜当前筛选：{len(rows)}｜显示前100条｜扫描池：{ctx['status']['active_universe']}")
    render_stock_table(rows[:100], "scanner_results")
    if rows:
        chosen = st.selectbox("选择股票进行研究", [row["symbol"] for row in rows if row.get("symbol")])
        if st.button("研究"):
            select_research_symbol(st.session_state, chosen)
            st.rerun()
