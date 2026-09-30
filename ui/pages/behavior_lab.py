"""Read-only Behavior Lab. It never fits models, downloads bars or calls an LLM."""
import json
import pandas as pd
import streamlit as st
from src.market_data.historical_store import HistoricalStore
from src.behavior.store import read_profile

def statistics_rows(pattern):
    rows=[]
    for split, horizons in pattern["statistics"].items():
        for horizon, stat in horizons.items():
            enough=stat["confidence"]!="INSUFFICIENT"
            rows.append(dict(split=split,horizon_days=int(horizon),samples=stat["sample_count"],
                independent_samples=stat["effective_samples"],confidence=stat["confidence"],
                positive_rate=stat["positive_rate"] if enough else None,
                mean_return=stat["mean_return"],median_return=stat["median_return"],
                MFE=stat["mfe"],MAE=stat["mae"],average_win=stat["average_win"],
                average_loss=stat["average_loss"],profit_loss_ratio=stat["profit_loss_ratio"],
                expectancy=stat["expectancy"]))
    return rows

@st.cache_data(ttl=30,show_spinner=False)
def load_profile(symbol,period):
    with HistoricalStore(read_only=True) as store:
        return read_profile(store,symbol,period)

def render(ctx=None):
    st.title("Behavior Lab")
    st.caption("本地历史行为研究 · 确认后才可用 · 不执行交易")
    columns=st.columns([2,1])
    with columns[0]:
        symbol=st.text_input("股票代码",value="600498.SH",key="behavior_symbol").strip().upper()
    with columns[1]:
        period=st.selectbox("周期",["1d","30m"],key="behavior_period")
    try:
        profile=load_profile(symbol,period)
    except Exception:
        st.info("本地结果尚不可读取，可能仍在更新。请在预计算完成后刷新。")
        return
    if profile is None:
        st.info("该股票尚无通过数据质量门槛的本地行为分析。")
        return
    st.caption("数据截至 "+profile["data_end_date"]+" · 当前匹配使用最后已观察收盘价，末端不是已确认拐点。")
    match=profile["current_match"]
    cells=st.columns(4)
    cells[0].metric("当前模式",match.get("pattern_id") or "未匹配")
    cells[1].metric("相似度",f'{match.get("similarity",0):.3f}')
    cells[2].metric("历史相似片段",match.get("sample_count",0))
    cells[3].metric("样本等级",match.get("confidence","INSUFFICIENT"))
    st.subheader("历史相似片段")
    history=match.get("historical_matches",[])
    if history:
        st.dataframe(pd.DataFrame([dict(date=x["date"],similarity=x["similarity"],
            feature_differences=json.dumps(x["key_feature_differences"],ensure_ascii=False)) for x in history]),hide_index=True,use_container_width=True)
    else:
        st.info("没有达到相似度门槛的历史片段。")
    patterns=profile["patterns"]
    st.subheader("1 / 3 / 5 / 10 / 20 交易日结果")
    st.caption("这是历史样本描述，不是预测胜率。有效独立样本不足5时隐藏上涨比例；未来结果与识别特征分开保存。")
    if patterns:
        ids=[p["pattern_id"] for p in patterns]
        selected=st.selectbox("查看模式",ids,index=ids.index(match["pattern_id"]) if match.get("pattern_id") in ids else 0)
        pattern=next(p for p in patterns if p["pattern_id"]==selected)
        st.dataframe(pd.DataFrame(statistics_rows(pattern)),hide_index=True,use_container_width=True)
    else:
        st.info("训练样本不足，尚无可拟合模式。")
    st.subheader("策略候选")
    candidates=profile["strategy_candidates"]
    if candidates:
        st.json(candidates)
        st.caption("候选仅从证据截止日之后可用，未在产生它的样本外区间再次回测并冒充独立验证。")
    else:
        st.info("NO_STATISTICALLY_USEFUL_PATTERN：尚无同时通过样本量、验证段、样本外和净收益门槛的候选。")
    st.subheader("样本外回测")
    result=profile["backtest"]
    st.dataframe(pd.DataFrame([result["metrics"]]),hide_index=True,use_container_width=True)
    if result["trades"]:
        st.dataframe(pd.DataFrame(result["trades"]),hide_index=True,use_container_width=True)
        equity=pd.DataFrame(result["equity"])
        equity["timestamp"]=pd.to_datetime(equity.timestamp)
        st.line_chart(equity.set_index("timestamp")["equity"])
    else:
        st.caption("没有符合事前选择规则的交易信号；0笔交易不代表策略已被证明有效。")
    with st.expander("成交假设与数据范围"):
        for note in [*profile.get("limitations",[]),*result.get("assumptions",[])]:
            st.write(note)
        st.json(profile.get("provenance",{}))
    probe=profile.get("execution_probe")
    if probe:
        with st.expander("成交路径验证（非策略业绩）"):
            st.caption("固定时点的一次测试买入，仅验证现金、交易单位、费用与退出流程，不构成模式策略。")
            st.dataframe(pd.DataFrame([probe["metrics"]]),hide_index=True,use_container_width=True)
            st.dataframe(pd.DataFrame(probe["fills"]),hide_index=True,use_container_width=True)
