"""Local selected-series features. No labels, network, backward fills or full-sample fits."""
import numpy as np
import pandas as pd
from .models import INDICES, clean
from ..behavior.segment_features import indicators

# Inputs are three relations: p4_bars, p4_universe and p4_calendar.
# A calendar grid prevents a return over a suspension from becoming a '1d' return.
BREADTH_SQL = """
WITH grid AS (
 SELECT u.symbol,c.day,u.listing_date,b.open,b.high,b.low,b.close,b.volume,b.amount,
        (u.listing_date IS NULL OR u.listing_date<=c.day) eligible
 FROM p4_universe u CROSS JOIN p4_calendar c
 LEFT JOIN p4_bars b ON b.symbol=u.symbol AND b.trade_date=c.day
), rolling AS (
 SELECT *,close/lag(close) OVER w-1 ret,
   close/lag(close,3) OVER w-1 ret3,
   CASE WHEN count(close) OVER w5=5 THEN avg(close) OVER w5 END ma5,
   CASE WHEN count(close) OVER w20=20 THEN avg(close) OVER w20 END ma20,
   CASE WHEN count(close) OVER w60=60 THEN avg(close) OVER w60 END ma60,
   CASE WHEN count(high) OVER prev20=20 THEN max(high) OVER prev20 END high20,
   CASE WHEN count(low) OVER prev20=20 THEN min(low) OVER prev20 END low20,
   CASE WHEN count(volume) OVER prev20=20 THEN avg(volume) OVER prev20 END volume20,
   CASE WHEN count(amount) OVER prev20=20 THEN avg(amount) OVER prev20 END amount20
 FROM grid WINDOW w AS (PARTITION BY symbol ORDER BY day),
 w5 AS (PARTITION BY symbol ORDER BY day ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
 w20 AS (PARTITION BY symbol ORDER BY day ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
 w60 AS (PARTITION BY symbol ORDER BY day ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
 prev20 AS (PARTITION BY symbol ORDER BY day ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
), moves AS (
 SELECT *,lag(ret) OVER (PARTITION BY symbol ORDER BY day) prior_ret,
 row_number() OVER (PARTITION BY day ORDER BY amount DESC NULLS LAST,symbol) amount_rank,
 count(close) OVER (PARTITION BY day) present,
 CASE WHEN count(ret) OVER rv=20 THEN stddev_pop(ret) OVER rv END volatility20
 FROM rolling WINDOW rv AS (PARTITION BY symbol ORDER BY day ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)
)
SELECT day,count(*) FILTER(WHERE eligible) expected_symbols,count(close) observed_symbols,
 count(ret) return_samples,count(ma5) ma5_samples,count(ma20) ma20_samples,count(ma60) ma60_samples,
 count(*) FILTER(WHERE ret>0) advances,count(*) FILTER(WHERE ret<0) declines,
 count(*) FILTER(WHERE ret=0) unchanged,
 count(*) FILTER(WHERE ret>.03) gain_gt3,count(*) FILTER(WHERE ret>.05) gain_gt5,
 count(*) FILTER(WHERE ret<-.03) loss_lt3,count(*) FILTER(WHERE ret<-.05) loss_lt5,
 avg((close>ma5)::INTEGER) above_ma5,avg((close>ma20)::INTEGER) above_ma20,
 avg((close>ma60)::INTEGER) above_ma60,
 avg((close>high20)::INTEGER) new_high20,avg((close<low20)::INTEGER) new_low20,
 median(ret) median_return,stddev_pop(ret) dispersion,sum(amount) total_amount,
 avg(CASE WHEN volume20>0 AND ret IS NOT NULL THEN (volume>volume20 AND ret>0)::INTEGER END) volume_up_ratio,
 avg(CASE WHEN volume20>0 AND ret IS NOT NULL THEN (volume>volume20 AND ret<0)::INTEGER END) volume_down_ratio,
 avg(CASE WHEN ret IS NOT NULL AND prior_ret IS NOT NULL THEN (ret>0 AND prior_ret>0)::INTEGER END) momentum_persistence1,
 avg((ret3>0)::INTEGER) momentum_persistence3,
 avg(CASE WHEN amount20>0 AND ret IS NOT NULL THEN (abs(ret)>.03 AND amount>amount20)::INTEGER END) active_large_move_ratio,
 avg(CASE WHEN amount20>0 AND volatility20 IS NOT NULL THEN (volatility20>=.025 AND ret>0 AND amount>amount20)::INTEGER END) high_volatility_participation,
 sum(CASE WHEN abs(ret)>.05 THEN amount ELSE 0 END)/nullif(sum(amount),0) large_move_amount_share,
 sum(CASE WHEN amount_rank<=greatest(1,ceil(present*.1)) THEN amount ELSE 0 END)/nullif(sum(amount),0) top_decile_amount_share
FROM moves GROUP BY day ORDER BY day
"""

def market_breadth(history, start, end):
    db=history.db
    # TEMP relations cannot replace any P2/P3 table.
    db.execute("CREATE OR REPLACE TEMP VIEW p4_universe AS SELECT symbol,listing_date FROM history_universe WHERE snapshot_id='p23b-daily-two-year'")
    db.execute("CREATE OR REPLACE TEMP TABLE p4_calendar AS SELECT day FROM historical_calendar WHERE day BETWEEN ? AND ?",[start,end])
    db.execute("""CREATE OR REPLACE TEMP VIEW p4_bars AS SELECT b.* FROM bars_daily b
        JOIN history_selection s ON s.symbol=b.symbol AND s.period='1d' AND s.source=b.source AND s.adjustment=b.adjustment
        JOIN p4_universe u ON u.symbol=b.symbol WHERE b.adjustment='raw' AND b.quality_status='VALID'""")
    result=db.execute(BREADTH_SQL).fetchdf()
    result['day']=pd.to_datetime(result.day)
    result['coverage']=result.observed_symbols/result.expected_symbols.clip(lower=1)
    result['return_coverage']=result.return_samples/result.expected_symbols.clip(lower=1)
    result['indicator_coverage']=result.ma60_samples/result.expected_symbols.clip(lower=1)
    result['advance_ratio']=result.advances/result.return_samples.replace(0,np.nan)
    result['decline_ratio']=result.declines/result.return_samples.replace(0,np.nan)
    result['amount_change']=result.total_amount.pct_change(fill_method=None)
    result['amount_ratio20']=result.total_amount/result.total_amount.shift().rolling(20).mean()
    result['breadth_persistence5']=result.above_ma20.rolling(5).mean()
    result['new_low_expansion']=result.new_low20-result.new_low20.shift(3)
    return result

def market_intraday(history, start, end):
    # Only three completed afternoon bars are needed for close-time acceleration.
    return history.db.execute("""WITH selected AS (
        SELECT b.symbol,b.trade_date,b.timestamp,b.close FROM bars_30m b
        JOIN history_selection s ON s.symbol=b.symbol AND s.period='30m' AND s.source=b.source AND s.adjustment=b.adjustment
        JOIN history_universe u ON u.symbol=b.symbol AND u.snapshot_id='p23b-daily-two-year'
        WHERE b.adjustment='raw' AND b.quality_status='VALID' AND b.trade_date BETWEEN ? AND ?
          AND CAST(b.timestamp AS TIME) IN (TIME '14:00',TIME '14:30',TIME '15:00')
    ), r AS (SELECT *,close/lag(close) OVER w-1 ret,
        timestamp-lag(timestamp) OVER w elapsed FROM selected
        WINDOW w AS (PARTITION BY symbol,trade_date ORDER BY timestamp)),
    a AS (SELECT *,lag(ret) OVER (PARTITION BY symbol,trade_date ORDER BY timestamp) prev_ret FROM r)
    SELECT trade_date AS day,count(*) FILTER(WHERE elapsed=INTERVAL '30 minutes' AND prev_ret IS NOT NULL) intraday_samples,
        avg(ret-prev_ret) FILTER(WHERE elapsed=INTERVAL '30 minutes') market_30m_acceleration,
        avg((ret>0)::INTEGER) FILTER(WHERE elapsed=INTERVAL '30 minutes') market_30m_up_ratio,
        stddev_pop(ret) FILTER(WHERE elapsed=INTERVAL '30 minutes') market_30m_dispersion
    FROM a WHERE CAST(timestamp AS TIME)=TIME '15:00' GROUP BY trade_date ORDER BY trade_date""",[start,end]).fetchdf()

def index_features(daily, minute):
    f=indicators(daily)
    for n in (1,3,5,20):
        f['return_'+str(n)]=f.close.pct_change(n,fill_method=None)
    f['ma20_slope']=f.ma20/f.ma20.shift(5)-1
    f['atr_ratio']=f.atr/f.close
    f['realized_volatility']=f.close.pct_change().rolling(20).std(ddof=0)*np.sqrt(252)
    for n in (20,60):
        f['drawdown_'+str(n)]=f.close/f.close.rolling(n).max()-1
    f['trend_persistence']= (f.close>f.ma20).astype(float).where(f.ma20.notna()).rolling(10).mean()
    f['above_ma20']=(f.close>f.ma20).astype(float).where(f.ma20.notna())
    f['above_ma60']=(f.close>f.ma60).astype(float).where(f.ma60.notna())
    m=indicators(minute,'30m')
    m['day']=m.timestamp.dt.normalize()
    m['trend_30m']=m.close/m.ma20-1
    m['macd_30m']=m.macd/m.close
    m['volatility_30m']=m.close.pct_change().rolling(20).std(ddof=0)
    m['acceleration_30m']=m.close.pct_change().diff()
    m['slots']=m.groupby('day').timestamp.transform('count')
    m=m[(m.timestamp.dt.hour==15)&(m.timestamp.dt.minute==0)&(m.slots==8)]
    cols=['trend_30m','macd_30m','volatility_30m','acceleration_30m']
    f['day']=f.timestamp.dt.normalize()
    f=f.merge(m[['day',*cols]],on='day',how='left')
    keys=['close','return_1','return_3','return_5','return_20','ma5','ma10','ma20','ma60','ma20_slope',
          'rsi','macd','macd_histogram','atr','atr_ratio','realized_volatility','drawdown_20','drawdown_60',
          'trend_persistence','above_ma20','above_ma60',*cols]
    return f[['day',*keys]]

def build_features(history, start='2024-10-01', end='2026-09-30'):
    breadth=market_breadth(history,start,end)
    intra=market_intraday(history,start,end); intra['day']=pd.to_datetime(intra.day)
    breadth=breadth.merge(intra,on='day',how='left')
    indices={}
    for symbol in INDICES:
        d=history.get_bars(symbol,'1d','2024-09-02',end)
        m=history.get_bars(symbol,'30m','2024-09-02',end)
        indices[symbol]=index_features(d,m).set_index('day') if len(d) and len(m) else pd.DataFrame()
    rows=[]
    for b in breadth.to_dict('records'):
        day=b.pop('day')
        states={s:clean(f.loc[day].to_dict()) for s,f in indices.items() if day in f.index}
        rows.append(dict(day=day,breadth=clean(b),index_state=states,
            limit_indicators={'status':'UNKNOWN','limit_up_count':None,'limit_down_count':None,
                              'reason':'Historical ST/IPO/special-session limit metadata incomplete.'}))
    return rows
