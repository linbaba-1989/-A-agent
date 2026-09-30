from datetime import datetime
from threading import Event
import pandas as pd
import pytest
from src.market_data.history_quality import HistoricalCalendar,validate_frame
from src.market_data.historical_store import HistoricalStore
from src.market_data.historical_sync import HistoricalSyncService
from src.market_data.xtdc_history import HistorySourceError

DAYS=['2026-09-28','2026-09-29','2026-09-30']


def bars(days=DAYS):
    return pd.DataFrame([dict(timestamp=pd.Timestamp(d),open=10.,high=12.,low=9.,close=11.,volume=100.,amount=1100.) for d in days])


@pytest.fixture
def store(tmp_path):
    s=HistoricalStore(tmp_path/'history.duckdb')
    s.set_calendar(HistoricalCalendar(DAYS,DAYS[0],DAYS[-1]))
    yield s
    s.close()


def put(s,f=None,**kw):
    return s.ingest('600498.SH','1d',bars() if f is None else f,DAYS[0],DAYS[-1],source='xtdc',provider='XtDataCenter Token',**kw)


def test_duplicate_rejection_and_idempotent_upsert(store):
    q=put(store,pd.concat([bars(),bars().iloc[:1]],ignore_index=True))
    assert q.duplicate_rows==1 and q.invalid_rows==1
    assert store.counts()['1d']==3
    put(store)
    assert store.counts()['1d']==3


def test_invalid_ohlc_rejected_not_filled(store):
    f=bars();f.loc[1,'low']=13
    q=put(store,f)
    assert q.invalid_rows==1 and len(q.frame)==2
    assert q.gaps[0]['classification']=='UNKNOWN_MISSING'
    assert q.gaps[0]['reason']=='UNKNOWN'


def test_missing_sessions_listing_evidence_and_unknown(store):
    q=put(store,bars(DAYS[1:]),listing_date=DAYS[1])
    assert q.gaps[0]['classification']=='EXPECTED_MISSING'
    q=put(store,bars([DAYS[0],DAYS[2]]))
    assert q.gaps[0]['classification']=='UNKNOWN_MISSING'


def test_verified_listing_metadata_reclassifies_only_prelisting_gaps(store):
    store.save_universe('test',[{'symbol':'600498.SH','list_date':None}])
    put(store,bars([DAYS[2]]))
    store.enrich_listing_dates({'600498.SH':DAYS[1]})
    rows=store.db.execute('SELECT trade_date,classification FROM history_gaps ORDER BY trade_date').fetchall()
    assert [r[1] for r in rows]==['EXPECTED_MISSING','UNKNOWN_MISSING']


def test_minute_gaps_do_not_invent_suspension_or_bars(store):
    f=bars(['2026-09-28 09:30','2026-09-28 11:30','2026-09-28 13:00','2026-09-28 15:00'])
    q=validate_frame(f,'1m',DAYS[0],DAYS[0],store.calendar())
    assert len(q.frame)==3 and q.invalid_rows==1
    assert q.gaps[0]['missing_count']==238 and q.gaps[0]['reason']=='UNKNOWN'


def test_raw_and_adjusted_series_never_mix(store):
    put(store)
    f=bars();f['close']=10
    put(store,f,adjustment='qfq')
    assert store.get_bars('600498.SH','1d',DAYS[0],DAYS[-1]).close.tolist()==[11]*3
    assert store.get_bars('600498.SH','1d',DAYS[0],DAYS[-1],'qfq').close.tolist()==[10]*3


def test_source_switch_requires_explicit_policy(store):
    put(store)
    with pytest.raises(ValueError,match='series_switch'):
        store.ingest('600498.SH','1d',bars(),DAYS[0],DAYS[-1],source='hithink',provider='Hithink',volume_unit='shares')
    assert store.provenance('600498.SH','1d')['source']=='xtdc'
    assert store.counts()['1d']==3


def test_provenance_mismatch_rejected(store):
    f=bars();f['source']='hithink'
    with pytest.raises(ValueError,match='provenance_mismatch'):put(store,f)


def test_duckdb_persistence_restart_and_range(tmp_path):
    path=tmp_path/'history.duckdb'
    with HistoricalStore(path) as s:
        s.set_calendar(HistoricalCalendar(DAYS,DAYS[0],DAYS[-1]));put(s);s.checkpoint()
    with HistoricalStore(path) as s:
        f=s.get_bars('600498.SH','1d',DAYS[1],DAYS[-1])
        assert len(f)==2 and set(f.adjustment)=={'raw'}
        p=s.provenance('600498.SH','1d')
        assert p['source']=='xtdc' and p['rows']==3 and p['quality']=='VALID'
        assert p['ingested_at'] and p['provider']=='XtDataCenter Token'


@pytest.mark.parametrize('symbol',['000001.SH','399001.SZ','399006.SZ','920433.BJ'])
def test_index_and_bj_bars(store,symbol):
    store.ingest(symbol,'1d',bars(),DAYS[0],DAYS[-1],source='xtdc',provider='XTDC')
    assert len(store.get_bars(symbol,'1d',DAYS[0],DAYS[-1]))==3


class Source:
    name='xtdc';provider='XTDC';volume_unit='lot'
    def __init__(self):self.calls=[];self.fail=set()
    def fetch(self,symbols,period,start,end,adjustment):
        self.calls.append((symbols,start,end))
        if any(s in self.fail for s in symbols):raise HistorySourceError('UPSTREAM_ERROR')
        f=bars();f=f[(f.timestamp>=start)&(f.timestamp<=end)]
        return {s:f.copy() for s in symbols}


def service(store,source,**kw):return HistoricalSyncService(store,source,sleeper=lambda _:None,**kw)


def test_resume_skips_completed_symbols_after_restart(store):
    src=Source();stop=Event()
    sync=service(store,src,batch_size=1,stop_event=stop,progress=lambda _:stop.set())
    sync.initial_backfill(['600498.SH','000001.SZ'],'1d',DAYS[0],DAYS[-1],job_id='resume')
    assert len(src.calls)==1
    stop.clear();sync.progress=lambda _:None;sync.resume('resume')
    assert len(src.calls)==2 and store.counts()['1d']==6
    sync.resume('resume');assert len(src.calls)==2


def test_failed_symbol_does_not_stop_batch_and_can_retry(store):
    src=Source();src.fail={'000001.SZ'};sync=service(store,src,max_attempts=1)
    r=sync.initial_backfill(['600498.SH','000001.SZ'],'1d',DAYS[0],DAYS[-1],job_id='retry')
    assert r['states']=={'DONE':1,'FAILED':1}
    assert store.db.execute("SELECT sum(missing_count) FROM history_gaps WHERE symbol='000001.SZ'").fetchone()[0]==3
    src.fail.clear();assert sync.resume('retry')['states']=={'DONE':2}
    assert store.db.execute("SELECT count(*) FROM history_gaps WHERE symbol='000001.SZ'").fetchone()[0]==0


def test_incremental_download_starts_after_last_timestamp(store):
    src=Source();sync=service(store,src)
    sync.initial_backfill(['600498.SH'],'1d',DAYS[0],DAYS[1],job_id='initial')
    sync.incremental_update(['600498.SH'],'1d',DAYS[-1],job_id='increment')
    assert src.calls[-1][1]==datetime(2026,9,30)
    assert store.counts()['1d']==3
    before=len(src.calls);sync.incremental_update(['600498.SH'],'1d',DAYS[-1],job_id='again')
    assert len(src.calls)==before


def test_rate_limit_backs_off_before_retry(store):
    src=Source();original=src.fetch;calls=[];waits=[]
    def fetch(*args):
        calls.append(1)
        if len(calls)==1:raise HistorySourceError('RATE_LIMITED')
        return original(*args)
    src.fetch=fetch
    sync=HistoricalSyncService(store,src,sleeper=waits.append)
    sync.initial_backfill(['600498.SH'],'1d',DAYS[0],DAYS[-1],job_id='rate')
    assert waits and waits[0]>=15 and store.counts()['1d']==3
    assert store.db.execute("SELECT count(*) FROM history_sync_events WHERE error_code='RATE_LIMITED'").fetchone()[0]==1


def test_behavior_dataset_is_local_only(store):
    put(store)
    data=store.get_two_year_behavior_dataset('600498.SH',DAYS[-1])
    assert data['network'] is False and len(data['daily'])==3 and data['bars_30m'].empty
    assert data['ready'] is False


def test_behavior_readiness_requires_entire_requested_window(store):
    store.set_calendar(HistoricalCalendar(pd.bdate_range('2024-10-01','2026-09-30'),'2024-10-01','2026-09-30'))
    put(store)
    store.ingest('600498.SH','30m',bars(['2026-09-30 10:00']), '2026-09-30 10:00:00','2026-09-30 10:00:00',source='xtdc',provider='XTDC')
    data=store.get_two_year_behavior_dataset('600498.SH',DAYS[-1])
    assert not data['ready'] and data['window_quality']['1d']['unknown_missing']>0


def test_unverified_calendar_range_rejected(store):
    with pytest.raises(ValueError,match='unverified'):
        validate_frame(bars(),'1d','2024-10-01',DAYS[-1],store.calendar())


def test_closed_session_window_completes_without_network(store):
    store.set_calendar(HistoricalCalendar(DAYS,'2026-09-26',DAYS[-1]))
    src=Source();sync=service(store,src)
    result=sync.initial_backfill(['600498.SH'],'1d','2026-09-26','2026-09-27',job_id='closed')
    assert result['states']=={'DONE':1} and not src.calls and store.counts()['1d']==0


def test_foundation_rejects_nonraw_sync_and_reused_job_range(store):
    sync=service(store,Source())
    with pytest.raises(ValueError,match='raw'):sync.initial_backfill(['600498.SH'],'1d',DAYS[0],DAYS[-1],job_id='x',adjustment='qfq')
    sync.initial_backfill(['600498.SH'],'1d',DAYS[0],DAYS[-1],job_id='x')
    with pytest.raises(ValueError,match='mismatch'):sync.initial_backfill(['600498.SH'],'1d',DAYS[1],DAYS[-1],job_id='x')


def test_large_plan_is_bulk_inserted_and_durable_without_network(store):
    stop=Event();stop.set();src=Source();sync=service(store,src,stop_event=stop)
    r=sync.initial_backfill([f'{i:06d}.SH' for i in range(12000)],'1d',DAYS[0],DAYS[-1],job_id='large-plan')
    assert r['states']=={'PENDING':12000} and not src.calls


def test_daily_fallback_keeps_single_source_and_minutes_do_not_fallback(store):
    primary=Source();primary.fail={'600498.SH'}
    backup=Source();backup.name='hithink';backup.volume_unit='shares'
    sync=service(store,primary,daily_fallbacks=[backup],max_attempts=1)
    sync.initial_backfill(['600498.SH'],'1d',DAYS[0],DAYS[-1],job_id='fallback')
    assert store.provenance('600498.SH','1d')['source']=='hithink'
    assert set(store.get_bars('600498.SH','1d',DAYS[0],DAYS[-1]).volume_unit)=={'shares'}
    before=len(backup.calls)
    sync.initial_backfill(['600498.SH'],'30m',DAYS[0],DAYS[-1],job_id='no-minute-fallback')
    assert len(backup.calls)==before and store.counts()['30m']==0


def test_intraday_incremental_keeps_prior_missing_slots(store):
    f=bars(['2026-09-28 10:00'])
    store.ingest('600498.SH','30m',f,DAYS[0],'2026-09-28 10:00:00',source='xtdc',provider='XTDC')
    src=Source()
    src.fetch=lambda *args:{'600498.SH':bars(['2026-09-28 15:00'])}
    sync=service(store,src)
    sync.incremental_update(['600498.SH'],'30m','2026-09-28 15:00:00',job_id='intraday')
    p=store.provenance('600498.SH','30m')
    assert p['rows']==2 and p['gaps']['UNKNOWN_MISSING']==6
