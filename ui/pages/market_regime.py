"""Local market context and historical family evidence, never a trading instruction."""
import json
import pandas as pd
import streamlit as st
from src.market_data.historical_store import HistoricalStore
from src.regime.service import get_current_market_regime
from src.regime.models import VERSION,RegimeConfig

@st.cache_data(ttl=30,show_spinner=False)
def load_current():
    return get_current_market_regime()

@st.cache_data(ttl=30,show_spinner=False)
def load_statistics(regime,as_of):
    with HistoricalStore(read_only=True) as h:
        rows=h.db.execute('''SELECT family,split,statistics_json FROM regime_strategy_statistics
            WHERE regime=? AND model_version=? AND config_key=? AND as_of<=? AND family IN ('TREND','SHORT')
            ORDER BY as_of,family''',[regime,VERSION,RegimeConfig().key,as_of]).fetchall()
    result=[]
    for family,split,raw in rows:
        stat=json.loads(raw)['metrics']
        result.append(dict(family=family,split=split,trades=stat['trades'],independent_days=stat['independent_entry_days'],
            confidence=stat['confidence'],win_rate=stat['win_rate'] if stat['independent_entry_days']>=5 else None,
            expectancy=stat['expectancy'],profit_loss_ratio=stat['profit_loss_ratio'],profit_factor=stat['profit_factor'],
            max_drawdown=stat['max_drawdown'],Sharpe=stat['Sharpe'],total_return=stat['total_return']))
    return result

def render_market_context():
    st.subheader('独立市场背景')
    try:
        current=load_current()
    except Exception:
        st.caption('市场状态暂不可读。个股模式保持原结果。')
        return
    st.write('市场：'+current['regime']+' · 截至 '+str(current.get('as_of')))
    st.caption('市场状态与个股匹配独立；市场强弱不会改变个股是否匹配。')

def render(ctx=None):
    st.title('Market Regime')
    st.caption('本地市场环境研究 · 日级状态仅收盘后生效 · 不执行交易')
    try:
        current=load_current()
    except Exception:
        st.info('本地状态尚不可读，可能仍在计算。')
        return
    st.caption('数据截至 '+str(current.get('data_end_time'))+' · 状态可用时间 '+str(current.get('as_of')))
    cells=st.columns(4)
    cells[0].metric('市场状态',current['regime'])
    for cell,label,key in zip(cells[1:],('趋势特征评分','短线环境评分','风险评分'),('trend_score','short_score','risk_score')):
        v=current.get(key); cell.metric(label,'UNKNOWN' if v is None else f'{v:.1f}')
    st.write('风险等级：'+current.get('risk_level','UNKNOWN'))
    st.write('策略族证据：'+current.get('strategy_preference','INSUFFICIENT_EVIDENCE'))
    st.caption('评分不是预期收益；confidence衡量数据覆盖与规则距离，不是盈利概率。')
    st.subheader('主要证据')
    st.json(current.get('evidence',{}))
    st.subheader('同一历史状态下的基准表现')
    st.caption('固定10股、独立账户等额资金；按入场信号时的状态筛选。train / validation / OOS分开，不代表全市场策略收益。')
    if current.get('as_of'):
        try: rows=load_statistics(current['regime'],current['as_of'])
        except Exception: rows=[]
        if rows: st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
        else: st.info('INSUFFICIENT_EVIDENCE：尚无可用的条件基准统计。')
    with st.expander('原始宽度、指数与数据限制'):
        st.json(current.get('breadth',{})); st.json(current.get('index_state',{}))
        st.json(current.get('limit_indicators',{}))
        for warning in current.get('warnings',[]): st.write(warning)
