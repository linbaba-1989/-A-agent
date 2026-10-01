from copy import deepcopy
import json
import duckdb
import numpy as np
import pandas as pd
import pytest
from src.regime.models import INDICES, available_at
from src.regime.engine import MarketRegimeEngine, classify_scores, cluster_diagnostic
from src.regime.features import BREADTH_SQL,index_features,market_intraday
from src.regime.evaluation import RegimeLookup,time_splits,compare_families,benchmark_signals,portfolio_summary
from src.regime.conditioning import conditional_statistics
from src.regime.store import RegimeStore
from src.regime.service import get_market_regime,get_regime_history
from src.market_data.historical_store import HistoricalStore

def feature(day='2025-01-06',bear=False):
    b=dict(expected_symbols=100,observed_symbols=100,return_samples=100,return_coverage=1.,indicator_coverage=1.,
        intraday_samples=100,above_ma5=.8,above_ma20=.8,above_ma60=.8,new_high20=.12,new_low20=.01,
        amount_ratio20=1.2,dispersion=.02,advance_ratio=.75,decline_ratio=.25,momentum_persistence1=.5,
        momentum_persistence3=.75,active_large_move_ratio=.15,high_volatility_participation=.15,large_move_amount_share=.25,top_decile_amount_share=.6,
        market_30m_acceleration=.001,new_low_expansion=0.,breadth_persistence5=.8,gain_gt3=30,loss_lt3=5,
        volume_up_ratio=.4,volume_down_ratio=.1)
    idx=dict(ma60=100,ma20_slope=.02,realized_volatility=.15,drawdown_60=-.01,trend_persistence=.9,
        above_ma20=1.,above_ma60=1.,trend_30m=.02,macd_30m=.01,volatility_30m=.005,return_3=.03)
    if bear:
        b.update(above_ma20=.1,above_ma60=.1,advance_ratio=.05,decline_ratio=.95,new_low20=.3,new_low_expansion=.2,
                 amount_ratio20=.4,dispersion=.07)
        idx.update(ma20_slope=-.04,drawdown_60=-.3,realized_volatility=.6,above_ma20=0.,above_ma60=0.,return_3=-.1)
    return dict(day=pd.Timestamp(day),breadth=b,index_state={s:dict(idx) for s in INDICES},limit_indicators={'status':'UNKNOWN'})

def test_no_lookahead_regime_and_directional_confirmation():
    rows=[feature(d) for d in pd.bdate_range('2025-01-01',periods=70)]
    e=MarketRegimeEngine(); full=e.evaluate(rows)
    assert full[0]['regime']=='MIXED' and full[1]['regime']=='TREND_STRONG'
    assert e.evaluate(rows[:40])==full[:40]
    altered=deepcopy(rows); altered[-1]=feature(rows[-1]['day'],True)
    assert e.evaluate(altered)[:-1]==full[:-1]
    assert e.evaluate(altered)[-1]['regime']=='RISK_OFF'

@pytest.mark.parametrize('values,expected',[
    ((80,40,20),'TREND_STRONG'),((40,80,30),'SHORT_HOT'),((55,40,35),'TREND_ROTATION'),
    ((40,40,40),'MIXED'),((90,90,80),'RISK_OFF')])
def test_rule_scores(values,expected):
    assert classify_scores(*values)==expected

def test_short_repair_and_score_bounds():
    assert classify_scores(40,55,45,repair=True)=='SHORT_REPAIR'
    good,bad=MarketRegimeEngine().evaluate([feature(),feature('2025-01-07',True)])
    assert good['trend_score']>bad['trend_score'] and good['risk_score']<bad['risk_score']
    assert good['short_score']>0 and bad['risk_level']=='HIGH'
    for r in (good,bad):
        assert all(0<=r[k]<=100 for k in ('trend_score','short_score','risk_score'))
        assert r['limit_indicators']['status']=='UNKNOWN'

@pytest.mark.parametrize('kind',['index','coverage','warmup','intraday'])
def test_missing_evidence_is_unknown(kind):
    row=feature()
    if kind=='index': del row['index_state'][INDICES[0]]
    elif kind=='coverage': row['breadth']['return_coverage']=.5
    elif kind=='warmup': row['breadth']['above_ma60']=None
    else: row['breadth']['intraday_samples']=10
    result=MarketRegimeEngine().evaluate([row])[0]
    assert result['regime']=='UNKNOWN' and result['confidence']==0 and result['trend_score'] is None

def breadth(missing=False):
    dates=pd.bdate_range('2025-01-01',periods=65)
    rows=[]
    for symbol,prices in [('A',100+np.arange(65)),('B',200-np.arange(65)),('C',np.full(65,100))]:
        for i,(day,price) in enumerate(zip(dates,prices)):
            if missing and symbol=='A' and i==63: continue
            rows.append(dict(symbol=symbol,trade_date=day,open=price,high=price,low=price,close=price,volume=100.,amount=10000.))
    db=duckdb.connect()
    db.register('p4_bars',pd.DataFrame(rows))
    db.register('p4_universe',pd.DataFrame(dict(symbol=['A','B','C'],listing_date=[dates[0]]*3)))
    db.register('p4_calendar',pd.DataFrame(dict(day=dates)))
    try: return db.execute(BREADTH_SQL).fetchdf()
    finally: db.close()

def test_breadth_counts_and_ma_above_ratios_use_valid_denominators():
    last=breadth().iloc[-1]
    assert (last.advances,last.declines,last.unchanged)==(1,1,1)
    assert last.above_ma5==pytest.approx(1/3) and last.above_ma60==pytest.approx(1/3)
    assert last.new_high20==pytest.approx(1/3) and last.new_low20==pytest.approx(1/3)

def test_missing_calendar_session_is_not_a_one_day_return():
    last=breadth(True).iloc[-1]
    assert last.return_samples==2 and last.advances==0
    assert last.ma60_samples==2

def test_completed_afternoon_market_acceleration_query():
    from types import SimpleNamespace
    db=duckdb.connect()
    try:
        db.execute("CREATE TABLE history_selection AS SELECT 'A' AS symbol,'30m' AS period,'local' AS source,'raw' AS adjustment")
        db.execute("CREATE TABLE history_universe AS SELECT 'A' AS symbol,'p23b-daily-two-year' AS snapshot_id")
        db.execute("""CREATE TABLE bars_30m AS SELECT 'A' AS symbol,DATE '2025-01-06' AS trade_date,
            t::TIMESTAMP AS timestamp,p::DOUBLE AS close,'local' AS source,'raw' AS adjustment,'VALID' AS quality_status
            FROM (VALUES ('2025-01-06 14:00:00',100),('2025-01-06 14:30:00',101),('2025-01-06 15:00:00',103)) v(t,p)""")
        r=market_intraday(SimpleNamespace(db=db),'2025-01-06','2025-01-06').iloc[0]
        assert r.intraday_samples==1 and r.market_30m_acceleration==pytest.approx(103/101-1-.01)
    finally: db.close()

def test_high_win_rate_does_not_override_negative_expectancy():
    times=pd.bdate_range('2025-01-01',periods=100)
    a=dict(metrics=dict(independent_entry_days=20,expectancy_lower=-.01,expectancy_upper=.02,
        profit_factor=.8,max_drawdown=-.1,win_rate=.9),equity=[dict(timestamp=t.isoformat(),equity=100000*1.001**i) for i,t in enumerate(times)])
    b=deepcopy(a); b['metrics']['win_rate']=.4
    b['equity']=[dict(timestamp=t.isoformat(),equity=100000) for t in times]
    assert compare_families(a,b)['preference']=='MIXED'

def test_daily_close_availability_and_no_future_lookup(tmp_path):
    path=tmp_path/'r.duckdb'
    rows=MarketRegimeEngine().evaluate([feature(),feature('2025-01-07')])
    with HistoricalStore(path) as h: RegimeStore(h).save(rows)
    assert get_market_regime('2025-01-06T14:59:00',db_path=path)['regime']=='UNKNOWN'
    assert get_market_regime('2025-01-06T15:00:00',db_path=path)['as_of'] is None
    assert get_market_regime('2025-01-06T15:01:00',db_path=path)['as_of']==rows[0]['as_of']
    assert get_market_regime('2025-01-07T09:30:00',db_path=path)['as_of']==rows[0]['as_of']
    assert len(get_regime_history('2025-01-06','2025-01-07',db_path=path))==2
    with HistoricalStore(path) as h:
        RegimeStore(h).save(rows)
        assert h.db.execute('SELECT count(*) FROM market_regimes').fetchone()[0]==2
        assert h.db.execute('SELECT count(*) FROM market_regimes WHERE data_end_time>=as_of').fetchone()[0]==0

def test_walk_forward_cluster_uses_training_only():
    rows=MarketRegimeEngine().evaluate([feature(d,i%4==0) for i,d in enumerate(pd.bdate_range('2025-01-01',periods=200))])
    split=time_splits([r['data_end_time'] for r in rows]); end=(split['train'][1]+pd.Timedelta(minutes=1)).isoformat()
    a=cluster_diagnostic(rows,end)
    changed=deepcopy(rows)
    for row in changed[150:]: row['risk_score']=99; row['trend_score']=1
    b=cluster_diagnostic(changed,end)
    assert a['centers']==b['centers'] and a['mean']==b['mean']
    assert split['train'][1]<split['validation'][0]<split['validation'][1]<split['out_of_sample'][0]
    assert a['used_by_classifier'] is False

def test_family_comparison_rejects_small_sample_even_high_win_rate():
    a={'metrics':{'independent_entry_days':2,'win_rate':1.}}
    b={'metrics':{'independent_entry_days':30,'win_rate':.5}}
    assert compare_families(a,b)['preference']=='INSUFFICIENT_EVIDENCE'

def test_both_families_demonstrably_bad_means_risk_off():
    a={'metrics':{'independent_entry_days':20,'expectancy_upper':-.01}}
    assert compare_families(a,a)['preference']=='RISK_OFF'

def test_conditioning_uses_occurrence_time_and_purges_future_labels():
    records=[dict(as_of='2025-01-06T15:01:00',regime='TREND_STRONG'),dict(as_of='2025-01-07T15:01:00',regime='RISK_OFF')]
    labels=[dict(signal_time='2025-01-07T10:00:00',known_at='2025-01-08T15:00:00',return_=.1,mfe=.2,mae=-.01),
            dict(signal_time='2025-01-07T15:00:00',known_at='2025-01-10T15:00:00',return_=1.,mfe=1.,mae=0)]
    result=conditional_statistics(labels,RegimeLookup(records),'2025-01-09T15:00:00')
    assert result['TREND_STRONG']['sample_count']==1 and result['RISK_OFF']['sample_count']==0
    assert result['TREND_STRONG']['confidence']=='INSUFFICIENT'

def test_future_outcomes_cannot_enter_regime_features_or_persistence(tmp_path):
    row=feature(); row['breadth']['future_return']=.9
    with pytest.raises(ValueError,match='allowlist'):
        MarketRegimeEngine().evaluate([row])
    record=MarketRegimeEngine().evaluate([feature()])[0]
    record['index_state'][INDICES[0]]['outcome']=1
    with HistoricalStore(tmp_path/'guard.duckdb') as h:
        with pytest.raises(ValueError,match='allowlist'):
            RegimeStore(h).save([record])
        assert h.db.execute('SELECT count(*) FROM market_regimes').fetchone()[0]==0

def test_nonfinite_feature_is_unknown_and_backdated_save_rejected(tmp_path):
    row=feature(); row['breadth']['dispersion']=float('nan')
    assert MarketRegimeEngine().evaluate([row])[0]['regime']=='UNKNOWN'
    record=MarketRegimeEngine().evaluate([feature()])[0]
    record['as_of']=record['data_end_time']
    with HistoricalStore(tmp_path/'time_guard.duckdb') as h:
        with pytest.raises(ValueError,match='backdated'):
            RegimeStore(h).save([record])

def test_regime_transaction_rollback_preserves_previous_snapshot(tmp_path,monkeypatch):
    rows=MarketRegimeEngine().evaluate([feature(),feature('2025-01-07')])
    with HistoricalStore(tmp_path/'atomic.duckdb') as h:
        store=RegimeStore(h); store.save(rows[:1]); original=store._insert
        def fail(table,values):
            if table=='market_regimes': raise RuntimeError('injected')
            return original(table,values)
        monkeypatch.setattr(store,'_insert',fail)
        with pytest.raises(RuntimeError): store.save(rows[1:])
        assert h.db.execute('SELECT count(*) FROM market_regime_features').fetchone()[0]==1
        assert h.db.execute('SELECT count(*) FROM market_regimes').fetchone()[0]==1

def test_benchmark_signals_are_invariant_to_future_prices():
    n=180; x=np.arange(n); close=100+x*.1+5*np.sin(x/6)
    f=pd.DataFrame(dict(timestamp=pd.bdate_range('2025-01-01',periods=n),open=close,high=close+1,low=close-1,close=close,
                        volume=np.full(n,1e6),amount=2e8*(1+.3*np.sin(x/5))))
    changed=f.copy(); changed.loc[150:,['open','high','low','close']]*=2
    for family in ('TREND','SHORT'):
        a=benchmark_signals(f,f,family); b=benchmark_signals(changed,changed,family)
        cut=f.timestamp.iloc[150].isoformat()
        assert [r for r in a if r['signal_time']<cut]==[r for r in b if r['signal_time']<cut]

def test_index_daily_and_30m_features_do_not_change_when_future_bars_arrive():
    dates=pd.bdate_range('2025-01-01',periods=100)
    def bars(times):
        c=100+np.arange(len(times))*.01+np.sin(np.arange(len(times))/9)
        return pd.DataFrame(dict(timestamp=times,open=c,high=c+1,low=c-1,close=c,volume=1000.,amount=1e6))
    daily=bars(dates)
    times=[d+pd.Timedelta(hours=h,minutes=m) for d in dates for h,m in ((10,0),(10,30),(11,0),(11,30),(13,30),(14,0),(14,30),(15,0))]
    minute=bars(times)
    full=index_features(daily,minute)
    prefix=index_features(daily.iloc[:80],minute.iloc[:640])
    pd.testing.assert_frame_equal(prefix,full.iloc[:80].reset_index(drop=True))
