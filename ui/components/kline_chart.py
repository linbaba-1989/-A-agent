from __future__ import annotations

import pandas as pd
import streamlit as st


def build_kline_figure(history: pd.DataFrame, days: int):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    frame = history.tail(days).copy()
    colors = ["#d92d20" if close >= opening else "#079455"
              for opening, close in zip(frame["open"], frame["close"])]
    figure = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=.025,
                           row_heights=[.76, .24])
    figure.add_trace(go.Candlestick(x=frame["date"], open=frame["open"], high=frame["high"],
                                    low=frame["low"], close=frame["close"], name="日K",
                                    increasing_line_color="#d92d20", increasing_fillcolor="#d92d20",
                                    decreasing_line_color="#079455", decreasing_fillcolor="#079455",
                                    hovertext=[f"日期 {date:%Y-%m-%d}<br>开 {opening:.2f}<br>高 {high:.2f}<br>低 {low:.2f}<br>收 {close:.2f}<br>涨跌幅 {change:.2f}%<br>成交量 {volume:,.0f}"
                                               for date, opening, high, low, close, change, volume in zip(
                                                   frame["date"], frame["open"], frame["high"], frame["low"],
                                                   frame["close"], frame["close"].pct_change().fillna(0) * 100,
                                                   frame["volume"])], hoverinfo="text"), row=1, col=1)
    for window, color in ((5, "#f79009"), (10, "#175cd3"), (20, "#7f56d9"), (60, "#344054")):
        series = history["close"].rolling(window).mean().tail(days)
        if series.notna().any():
            figure.add_trace(go.Scatter(x=frame["date"], y=series, name=f"MA{window}",
                                        line={"width": 1.2, "color": color}, hovertemplate="%{y:.2f}"), row=1, col=1)
    figure.add_trace(go.Bar(x=frame["date"], y=frame["volume"], marker_color=colors,
                            name="成交量", hovertemplate="%{y:,.0f}"), row=2, col=1)
    figure.update_layout(height=540, margin=dict(l=8, r=8, t=28, b=8), paper_bgcolor="#fff",
                         plot_bgcolor="#fff", hovermode="x unified", legend=dict(orientation="h", y=1.02),
                         xaxis_rangeslider_visible=False,
                         font={"family": '"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Segoe UI",Arial,sans-serif'})
    figure.update_xaxes(showgrid=False, rangebreaks=[dict(bounds=["sat", "mon"])])
    figure.update_yaxes(showgrid=True, gridcolor="#eef0f3", zeroline=False)
    return figure


def render_kline(history: pd.DataFrame, days: int) -> None:
    if history is None or history.empty:
        st.info("暂无历史K线数据")
        return
    st.plotly_chart(build_kline_figure(history, days), width="stretch",
                    config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]})
