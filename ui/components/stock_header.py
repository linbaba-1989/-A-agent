import streamlit as st
from html import escape

from ui.view_models import cn_change_color, format_amount, format_number, normalize_stock_name


def stock_header_view(symbol: str, quote: dict, name: str = "") -> dict:
    last = quote.get("lastPrice", quote.get("last_price", "unavailable"))
    close = quote.get("lastClose", quote.get("previous_close", "unavailable"))
    try:
        change = float(last) - float(close)
        change_pct = change / float(close) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        change = change_pct = "unavailable"
    return {"symbol": symbol, "name": normalize_stock_name(name, symbol), "last": format_number(last),
            "change": "--" if change == "unavailable" else f"{change:+.2f}",
            "change_pct": "--" if change_pct == "unavailable" else f"{change_pct:+.2f}%",
            "color": cn_change_color(change), "quote_time": quote.get("timestamp", "--")}


def render_stock_header(symbol: str, quote: dict, name: str = "", market_status: str = "--") -> None:
    view = stock_header_view(symbol, quote, name)
    st.markdown(f"<div class='panel'><div class='eyebrow'>{view['name']} · {symbol}</div>"
                f"<div class='price' style='color:{view['color']}'>{view['last']}</div>"
                f"<b style='color:{view['color']}'>{view['change']}　{view['change_pct']}</b>"
                f"<span style='float:right;color:#667085'>市场：{market_status}　行情时间：{view['quote_time']}</span></div>",
                unsafe_allow_html=True)
    labels = [("今开", "open"), ("最高", "high"), ("最低", "low"), ("昨收", "lastClose"),
              ("成交量", "volume"), ("成交额", "amount"), ("换手率", "turnover_rate"), ("振幅", "amplitude")]
    cells = []
    for label, field in labels:
        value = quote.get(field)
        if field == "amount": shown = format_amount(value)
        elif field == "volume":
            try: shown = f"{float(value) / 10_000:.2f}万手" if abs(float(value)) >= 10_000 else f"{float(value):.0f}手"
            except (TypeError, ValueError): shown = "--"
        elif field in ("turnover_rate", "amplitude"):
            shown = format_number(value); shown = shown if shown == "--" else shown + "%"
        else: shown = format_number(value)
        cells.append(f"<div><span>{escape(label)}</span><b>{escape(shown)}</b></div>")
    st.markdown("<div class='quote-grid'>" + "".join(cells) + "</div>", unsafe_allow_html=True)
