import streamlit as st

from ui.view_models import cn_change_color, display_value


def render_stock_header(symbol: str, quote: dict, name: str = "") -> None:
    last = quote.get("lastPrice", quote.get("last_price", "unavailable"))
    close = quote.get("lastClose", quote.get("previous_close", "unavailable"))
    try:
        change = float(last) - float(close)
        change_pct = change / float(close) * 100
    except (TypeError, ValueError, ZeroDivisionError):
        change = change_pct = "unavailable"
    color = cn_change_color(change)
    st.markdown(f"<div class='panel'><div class='eyebrow'>{name or '证券'} · {symbol}</div>"
                f"<div class='price' style='color:{color}'>{display_value(last)}</div>"
                f"<b style='color:{color}'>{display_value(change)}　{display_value(change_pct, '%')}</b></div>",
                unsafe_allow_html=True)
    labels = [("今开", "open"), ("最高", "high"), ("最低", "low"), ("昨收", "lastClose"),
              ("成交量", "volume"), ("成交额", "amount"), ("换手率", "turnover_rate"), ("振幅", "amplitude")]
    cols = st.columns(8)
    for col, (label, field) in zip(cols, labels):
        col.metric(label, display_value(quote.get(field)))
