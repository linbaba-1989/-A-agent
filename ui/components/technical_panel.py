from html import escape

import streamlit as st

from ui.view_models import format_amount, format_number


def _shown(value, suffix=""):
    result = format_number(value)
    return result if result == "--" else result + suffix


def _row(label: str, value: str, extra: str = "") -> str:
    return f"<div class='tech-row'><span>{escape(label)}</span><b>{escape(value)}{escape(extra)}</b></div>"


def render_technical_panel(facts: dict) -> None:
    rows = ["<div class='side-panel-title'>技术状态</div>",
            _row("趋势", str(facts.get("trend", "中性"))),
            "<div class='tech-note'>规则：价与 MA5/10/20 的顺序结构</div>"]
    for window in (5, 10, 20, 60):
        value = facts.get(f"ma{window}")
        try: comparison = " ✓" if float(facts["last_price"]) > float(value) else ""
        except (TypeError, ValueError): comparison = ""
        rows.append(_row(f"MA{window}", _shown(value), comparison))
    rows.extend([_row("ATR / ATR%", f"{_shown(facts.get('atr14'))} / {_shown(facts.get('atr_pct'), '%')}"),
                 _row("20日高 / 低", f"{_shown(facts.get('high_20d'))} / {_shown(facts.get('low_20d'))}"),
                 _row("20日区间位置", _shown(facts.get("range_position_20d"), "%")),
                 _row("突破状态", str(facts.get("breakout_status", "--"))),
                 "<div class='side-panel-title tech-section'>量价</div>",
                 _row("今日成交额", format_amount(facts.get("amount"))),
                 _row("换手率 / 量比", f"{_shown(facts.get('turnover_rate'), '%')} / {_shown(facts.get('volume_ratio'))}"),
                 _row("成交量", f"{format_number(facts.get('volume'), 0)} 手"),
                 "<div class='side-panel-title tech-section'>分钟涨速</div>",
                 _row("1m / 3m / 5m", " / ".join(_shown(facts.get(f"speed_{minute}m"), "%") for minute in (1, 3, 5)))])
    if facts.get("market_status") != "open":
        rows.append("<div class='tech-note'>分钟涨速仅交易时段有效</div>")
    st.markdown("<div class='tech-panel'>" + "".join(rows) + "</div>", unsafe_allow_html=True)
