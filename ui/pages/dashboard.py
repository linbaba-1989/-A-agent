import html
import streamlit as st

from ui.view_models import cn_change_color, display_value, format_amount, format_number


def _table(rows):
    labels = ("代码", "名称", "最新", "涨幅", "1m", "3m", "5m", "换手", "成交额", "信号")
    keys = ("symbol", "name", "lastPrice", "change_pct", "speed_1m", "speed_3m",
            "speed_5m", "turnover_rate", "amount", "breakout_status")
    body = "<tr><td class='empty-row' colspan='10'>尚未执行扫描</td></tr>"
    if rows:
        body = ""
        for row in rows[:15]:
            cells = []
            for index, key in enumerate(keys):
                value = row.get(key)
                color = cn_change_color(value) if 3 <= index <= 6 else "#17202e"
                if key in ("lastPrice", "change_pct", "speed_1m", "speed_3m", "speed_5m", "turnover_rate"):
                    shown = format_number(value)
                elif key == "amount":
                    shown = format_amount(value)
                else:
                    shown = display_value(value)
                cells.append(f"<td style='color:{color}'>{html.escape(shown)}</td>")
            body += "<tr>" + "".join(cells) + "</tr>"
    head = "".join(f"<th>{label}</th>" for label in labels)
    st.markdown(f"<table class='terminal-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
                unsafe_allow_html=True)


def _market_panel(status, rows):
    counts, valid = [0, 0, 0], 0
    for row in rows:
        try: change = float(row.get("change_pct"))
        except (TypeError, ValueError): continue
        valid += 1
        counts[0 if change > 0 else 1 if change < 0 else 2] += 1
    shown = counts if valid else ["--", "--", "--"]
    lines = (("上涨家数", shown[0], "quote-up"), ("下跌家数", shown[1], "quote-down"),
             ("平盘", shown[2], ""), ("涨停 / 跌停", "-- / --", ""),
             ("扫描池", status["active_universe"], ""), ("市场", status["market"], ""))
    st.markdown("<div class='side-panel-title'>市场扫描概览</div>" + "".join(
        f"<div class='ai-line'><span>{label}</span><b class='{css}'>{value}</b></div>"
        for label, value, css in lines), unsafe_allow_html=True)


def _ai_panel(ctx):
    names = (("technical_analyst", "技术分析"), ("fundamental_event_analyst", "基本面/事件"),
             ("sentiment_analyst", "市场情绪"), ("risk_officer", "风险官"),
             ("chief_researcher", "总研究员"))
    parts = ["<div class='side-panel-title'>AI研究院</div>"]
    for role, label in names:
        state = ctx["workforce"].router.role_states.get(role, "idle")
        ready = state not in ("working", "error")
        css, text = ("dot-ok", "Ready") if ready else ("dot-warn", state)
        parts.append(f"<div class='ai-line'><span>{label}</span><b class='{css}'>● {text}</b></div>")
    records = st.session_state.get("research_history", [])
    parts.append("<div style='padding-top:7px;font-size:12px;color:#667085'>最近研究</div>")
    parts.append("<div style='font-size:12px;padding-top:4px'>暂无研究记录</div>" if not records else
                 f"<div style='font-size:12px;padding-top:4px'>{html.escape(records[0].get('symbol','--'))}　{records[0].get('analysis_mode','--')}</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render(ctx: dict) -> None:
    status, rows = ctx["status"], st.session_state.get("scan_rows", [])
    st.markdown("<div class='index-strip'><b>上证　--</b><b>深证　--</b><b>创业板　--</b>"
                "<span style='color:#98a2b3'>指数数据暂未接入</span></div>", unsafe_allow_html=True)
    elapsed = st.session_state.get("last_scan_elapsed")
    elapsed = f"{elapsed:.2f}s" if isinstance(elapsed, (int, float)) else "--"
    latest = str(status["last_quote_time"]).replace("T", " ")
    items = (("行情源", f"{status['provider']} <span class='dot-ok'>●</span>"), ("原始", status["raw_universe"]),
             ("可扫描", status["active_universe"]), ("有效行情", status["valid_quotes"]),
             ("扫描耗时", elapsed), ("QMT备用", "×" if "unavailable" in str(status["qmt_fallback"]) else "●"),
             ("最近行情", latest))
    st.markdown("<div class='system-strip'>" + "".join(
        f"<span class='system-item'>{label}<b>{value}</b></span>" for label, value in items) + "</div>", unsafe_allow_html=True)
    left, right = st.columns([7, 3], gap="small")
    with left:
        title = "最近交易日候选" if status["market"] == "已收盘" else "今日强势候选"
        st.markdown(f"<div class='side-panel-title'>{title}</div>", unsafe_allow_html=True)
        _table(rows)
    with right:
        with st.container(border=True): _market_panel(status, rows)
        with st.container(border=True): _ai_panel(ctx)
    st.markdown("<div class='side-panel-title' style='margin-top:7px'>最近AI研究</div>", unsafe_allow_html=True)
    if not st.session_state.get("research_history"): st.caption("暂无研究记录")
