import pandas as pd
import streamlit as st

from ui.view_models import (cn_change_color, format_amount, format_number, format_security_status,
                            normalize_stock_name)


COLUMNS = {"symbol": "代码", "name": "名称", "lastPrice": "最新", "change_pct": "涨幅%",
           "speed_1m": "1m%", "speed_3m": "3m%", "speed_5m": "5m%",
           "turnover_rate": "换手%", "amount": "成交额", "ma5": "MA5", "ma10": "MA10",
           "ma20": "MA20", "security_status": "状态"}


def render_stock_table(rows: list[dict], key: str = "stocks") -> None:
    empty = not rows
    frame = pd.DataFrame(rows or [{key: "--" for key in COLUMNS}])
    visible = [column for column in COLUMNS if column in frame.columns]
    frame = frame[visible].rename(columns=COLUMNS)
    if empty:
        frame.loc[0, "状态"] = "尚未执行扫描"
    else:
        if "名称" in frame:
            frame["名称"] = [normalize_stock_name(name, str(symbol) if symbol else "--")
                               for name, symbol in zip(frame["名称"], frame.get("代码", [None] * len(frame)))]
        if "状态" in frame:
            frame["状态"] = frame["状态"].map(format_security_status)
    color_columns = [column for column in ("涨幅%", "1m%", "3m%", "5m%") if column in frame]
    display = frame.style.map(lambda value: f"color:{cn_change_color(value)}", subset=color_columns)
    display = display.format({"最新": format_number, "涨幅%": format_number,
                              "1m%": format_number, "3m%": format_number, "5m%": format_number,
                              "换手%": format_number, "成交额": format_amount,
                              "MA5": format_number, "MA10": format_number, "MA20": format_number}, na_rep="--")
    st.dataframe(display, hide_index=True, width="stretch", height=620, key=key,
                 column_config={"名称": st.column_config.TextColumn(width="medium"),
                                "状态": st.column_config.TextColumn(width="small")})
