"""Frozen benchmark families and chronological evaluation, never regime inputs."""
from bisect import bisect_right
from statistics import NormalDist
import numpy as np
import pandas as pd
from ..behavior.segment_features import indicators
from ..backtest.engine import BacktestEngine, TradingRules, performance
from .models import REGIMES, clean
from .store import statistics_record

COHORT=('600498.SH','600519.SH','600036.SH','600900.SH','002053.SZ',
        '000001.SZ','000651.SZ','002475.SZ','300750.SZ','300059.SZ')
FAMILIES=('TREND','SHORT')

class RegimeLookup:
    def __init__(self, records):
        rows=sorted(records,key=lambda r:r['as_of'])
        self.times=[pd.Timestamp(r['as_of']) for r in rows]
        self.rows=rows
    def at(self, value):
        i=bisect_right(self.times,pd.Timestamp(value))-1
        return self.rows[i]['regime'] if i>=0 else 'UNKNOWN'

def time_splits(days):
    days=sorted(pd.to_datetime(days).normalize().unique())
    if len(days)<100: raise ValueError('insufficient_market_history')
    a,b=int(len(days)*.6),int(len(days)*.8)
    return {name:(pd.Timestamp(chunk[0]),pd.Timestamp(chunk[-1])+pd.Timedelta(hours=15))
            for name,chunk in zip(('train','validation','out_of_sample'),(days[:a],days[a:b],days[b:]))}

def benchmark_signals(frame, index_frame, family):
    if family not in FAMILIES: raise ValueError('unknown_family')
    f=indicators(frame); idx=index_frame.set_index('timestamp').close
    f['index_close']=f.timestamp.map(idx)
    ret=f.close.pct_change(fill_method=None)
    f['relative20']=f.close.pct_change(20,fill_method=None)-f.index_close.pct_change(20,fill_method=None)
    f['relative3']=f.close.pct_change(3,fill_method=None)-f.index_close.pct_change(3,fill_method=None)
    prior_high=f.high.shift().rolling(20).max()
    amount_ratio=f.amount/f.amount.shift().rolling(20).mean()
    liquid=(f.amount.shift().rolling(20).mean()>=100_000_000)&(f.volume>0)
    if family=='TREND':
        condition=liquid&(f.close>f.ma20)&(f.ma20>f.ma60)&(f.ma20>f.ma20.shift(5))&(f.relative20>0)&(
            (f.close>prior_high)|((f.close/f.ma20-1).between(0,.025)&(ret>0)))
        policy=dict(stop_loss=.08,take_profit=.20,max_holding_days=20)
    else:
        condition=liquid&(ret>0)&(f.close.pct_change(3,fill_method=None)>.02)&(f.relative3>0)&(amount_ratio>1.1)&(f.volatility.between(.008,.06))
        policy=dict(stop_loss=.04,take_profit=.08,max_holding_days=3)
    # Edge triggering avoids repeatedly entering the same uninterrupted signal.
    edges=condition&~condition.shift(fill_value=False)
    return [dict(action='BUY',signal_time=(t.normalize()+pd.Timedelta(hours=15,minutes=1)).isoformat(),**policy)
            for t in f.loc[edges,'timestamp']]

def portfolio_summary(results):
    curves=[]; trades=[]
    for symbol,result in results.items():
        curve=pd.DataFrame(result['equity']).set_index('timestamp').equity
        curves.append(curve.rename(symbol))
        trades.extend(dict(t,symbol=symbol) for t in result['trades'])
    if not curves: raise ValueError('empty_benchmark_cohort')
    # Fixed equal capital per stock; cash stays in inactive accounts.
    equity=pd.concat(curves,axis=1).sort_index().ffill().fillna(100000.).sum(axis=1)
    rows=[dict(timestamp=t,equity=float(v)) for t,v in equity.items()]
    trades.sort(key=lambda t:(t['exit_time'],t['symbol']))
    metrics=performance(rows,trades,100000.*len(curves))
    grouped={}
    for t in trades:
        grouped.setdefault(t['entry_signal_time'][:10],[]).append(t['net_return'])
    samples=np.array([np.mean(v) for v in grouped.values()])
    n=len(samples); z=NormalDist().inv_cdf(1-.05/(2*14))
    error=z*samples.std(ddof=1)/np.sqrt(n) if n>1 else None
    metrics.update(independent_entry_days=n,entry_day_mean=float(samples.mean()) if n else None,
        expectancy_lower=float(samples.mean()-error) if error is not None else None,
        expectancy_upper=float(samples.mean()+error) if error is not None else None,
        confidence='INSUFFICIENT' if n<5 else 'LOW_CONFIDENCE' if n<10 else 'NORMAL',
        open_positions=sum(r['open_quantity']>0 for r in results.values()),
        blocked_orders=sum(len(r['blocked_orders']) for r in results.values()))
    return clean(dict(metrics=metrics,equity=rows,trades=trades,
        assumptions=['Ten fixed P3 stocks, equal initial capital; no market-wide strategy-performance claim.',
                     'Same P3 execution engine; estimated ordinary-board limits, raw-price returns.',
                     'Entry-day grouping reduces same-day cross-stock duplication; residual serial dependence remains.',
                     'Open positions are marked to market, not artificially closed at split boundaries.']))

def compare_families(trend, short, minimum=10):
    t,s=trend['metrics'],short['metrics']
    if min(t['independent_entry_days'],s['independent_entry_days'])<minimum:
        return dict(preference='INSUFFICIENT_EVIDENCE',reason='Too few independent entry days in one or both families.')
    if t['expectancy_upper']<0 and s['expectancy_upper']<0:
        return dict(preference='RISK_OFF',reason='Both family expectancy upper bounds are below zero.')
    te=pd.Series({r['timestamp']:r['equity'] for r in trend['equity']})
    se=pd.Series({r['timestamp']:r['equity'] for r in short['equity']})
    paired=pd.concat([te.pct_change(),se.pct_change()],axis=1).dropna()
    difference=paired.iloc[:,0]-paired.iloc[:,1]
    # Non-overlapping 5-session blocks avoid treating adjacent daily returns as independent.
    complete=difference.iloc[:len(difference)//5*5]
    blocks=complete.groupby(np.arange(len(complete))//5).sum()
    error=3*blocks.std(ddof=1)/np.sqrt(len(blocks)) if len(blocks)>=10 else None
    if error is None: return dict(preference='INSUFFICIENT_EVIDENCE',reason='Fewer than ten paired five-session blocks.')
    lower,upper=float(blocks.mean()-error),float(blocks.mean()+error)
    preference='MIXED'
    if lower>0 and t['expectancy_lower']>0 and (t['profit_factor'] or 0)>1 and t['max_drawdown']>=s['max_drawdown']:
        preference='TREND_PREFERRED'
    elif upper<0 and s['expectancy_lower']>0 and (s['profit_factor'] or 0)>1 and s['max_drawdown']>=t['max_drawdown']:
        preference='SHORT_PREFERRED'
    return dict(preference=preference,paired_block_difference_lower=lower,paired_block_difference_upper=upper,
        reason='Joint expectancy, net profit factor, drawdown and paired portfolio-return comparison; win rate alone is insufficient.')

def evaluate_benchmarks(history, records):
    lookup=RegimeLookup(records); days=[pd.Timestamp(r['data_end_time']).normalize() for r in records]
    splits=time_splits(days); frames={s:history.get_bars(s,'1d',str(days[0].date()),str(days[-1].date())) for s in COHORT}
    reference=history.get_bars('000001.SH','1d',str(days[0].date()),str(days[-1].date()))
    signals={s:{family:benchmark_signals(f,reference,family) for family in FAMILIES} for s,f in frames.items()}
    output={}; statistics=[]
    for phase,(start,end) in splits.items():
        output[phase]={}
        for regime in (*REGIMES,'ALL'):
            families={}
            for family in FAMILIES:
                results={}
                for symbol,frame in frames.items():
                    window=frame[frame.timestamp.between(start,end)].copy()
                    if window.empty: raise ValueError('incomplete_benchmark_window')
                    selected=[s for s in signals[symbol][family] if start<=pd.Timestamp(s['signal_time'])<=end+pd.Timedelta(minutes=1)
                        and (regime=='ALL' or lookup.at(s['signal_time'])==regime)]
                    rules=TradingRules(regular_limit_pct=.2 if symbol.startswith(('300','301')) else .1,allow_estimated_limits=True)
                    results[symbol]=BacktestEngine(rules).run(window,selected)
                families[family]=portfolio_summary(results)
                statistics.append(statistics_record(regime,family,phase,end.isoformat(),families[family]))
            comparison=compare_families(families['TREND'],families['SHORT'])
            comparison['descriptive_preference']=comparison['preference']
            if phase=='out_of_sample':
                train=output['train'][regime]['comparison']['descriptive_preference']
                validation=output['validation'][regime]['comparison']['descriptive_preference']
                comparison['selected_before_oos']=train if train==validation else 'INSUFFICIENT_EVIDENCE'
                if comparison['preference'] in ('TREND_PREFERRED','SHORT_PREFERRED') and comparison['preference']!=comparison['selected_before_oos']:
                    comparison['preference']='INSUFFICIENT_EVIDENCE'
                    comparison['reason']+=' OOS advantage was not selected consistently in train and validation.'
            families['comparison']=comparison
            output[phase][regime]=families
            statistics.append(statistics_record(regime,'COMPARISON',phase,end.isoformat(),comparison))
    return clean(dict(cohort=list(COHORT),splits={k:[x.isoformat() for x in v] for k,v in splits.items()},
        results=output,shuffle=False,threshold_tuning=False)),statistics
