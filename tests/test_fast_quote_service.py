from datetime import datetime,timedelta
from pathlib import Path
from urllib.error import URLError
from dataclasses import replace
import time
from src.market_clock import BEIJING_TZ
from src.market_data.fast_quote_service import FastQuoteService,RequestHealth
from src.market_data.free_provider import FreeMarketDataProvider
from src.market_data.adapters.realtime import TencentRealtimeProvider,SinaRealtimeProvider

NOW=datetime(2026,9,24,10,0,tzinfo=BEIJING_TZ)
TX=(Path(__file__).parent/'fixtures/free_market/tencent.txt').read_text(encoding='utf-8').splitlines()[0]
SINA=(Path(__file__).parent/'fixtures/free_market/sina.txt').read_text(encoding='utf-8').splitlines()[0]

def service(fail=False):
    clock=[NOW];timer=[0.];calls=[];bad=[fail]
    def tx(url,**kwargs):
        calls.append(url)
        if bad[0]:raise TimeoutError()
        return TX
    a=TencentRealtimeProvider(transport=tx,clock=lambda:clock[0]);a.health=RequestHealth()
    b=SinaRealtimeProvider(transport=lambda *a,**k:SINA,clock=lambda:clock[0]);b.health=RequestHealth()
    provider=FreeMarketDataProvider(primary=a,fallback=b,timer=lambda:timer[0])
    lane=FastQuoteService(provider,clock=lambda:clock[0],timer=lambda:timer[0],autostart=False)
    lane.subscribe('view','600498.SH')
    return lane,clock,timer,calls,bad


def test_success_schema_provenance_and_duplicate_poll():
    lane,clock,timer,calls,_=service()
    p=lane.poll_once();q=p['quotes']['600498.SH']
    assert q['channel']=='fast' and q['source']=='tencent' and q['trade_date']=='2026-09-24'
    assert q['volume_shares']==41878700 and q['amount_cny']==1740050000
    assert q['received_at']==clock[0].isoformat()
    assert lane.poll_once() is None and len(calls)==1
    assert lane.samples['600498.SH'][0]['cumulative_volume']==41878700


def test_inactive_stale_quote_does_not_fail_request_health_or_fallback():
    lane,clock,timer,calls,_=service()
    for i in range(10):
        timer[0]=i*3;clock[0]=NOW+timedelta(seconds=i*3);p=lane.poll_once()
    assert p['quotes']['600498.SH']['quote_status']=='STALE'
    assert lane.provider.primary.health.state=='HEALTHY'
    assert lane.provider.active_source=='tencent'
    assert lane.provider.fallback.health.consecutive_successes==0
    assert len(lane.samples['600498.SH'])==1  # same quote timestamp deduplicated


def test_single_timeout_no_switch_continuous_timeout_fallback_and_backoff():
    lane,clock,timer,calls,bad=service(True)
    p=lane.poll_once()
    assert p['source']=='tencent' and lane.next_due==2
    timer[0]=1;assert lane.poll_once() is None
    timer[0]=2;lane.poll_once();timer[0]=6;p=lane.poll_once()
    assert p['source']=='sina' and len(calls)==3
    assert lane.provider.primary.health.consecutive_failures==3


def test_recovery_keeps_existing_probe_and_hysteresis():
    lane,clock,timer,calls,bad=service(True)
    for t in (0,2,6):timer[0]=t;lane.poll_once()
    bad[0]=False
    timer[0]=7;assert lane.poll_once()['source']=='sina'
    for t in (36,66):
        timer[0]=t;assert lane.poll_once()['source']=='sina'
    timer[0]=96;assert lane.poll_once()['source']=='tencent'


def test_subscription_switch_watchlist_union_cap_and_cleanup():
    lane,clock,timer,calls,_=service()
    lane.subscribe('view','600000.SH',['600498.SH'])
    assert lane.symbols()==['600000.SH','600498.SH']
    lane.subscribe('view','600001.SH',['600498.SH'])
    assert '600000.SH' not in lane.symbols() and '600498.SH' in lane.symbols()
    result=lane.subscribe('view','600498.SH',[f'{i:06d}.SZ' for i in range(100)])
    assert len(lane.symbols())==50 and result['subscription_status']=='DEGRADED_LIMIT'
    lane.unsubscribe('view');assert lane.symbols()==[]
    assert lane.poll_once() is None


def test_restart_ring_empty_and_channels_are_separate():
    a,_,_,_,_=service();b,_,_,_,_=service()
    a.poll_once();assert b.quotes=={} and b.buffer.points('600498.SH')==[]
    assert a.buffer is not b.buffer and a.provider is not b.provider
    assert a.provider.primary.health is not b.provider.primary.health


def test_no_duplicate_worker():
    lane,*_=service();lane.unsubscribe('view');lane.start();worker=lane.worker
    lane.start();assert lane.worker is worker
    lane.close();assert not worker.is_alive()


def test_quote_progress_uses_existing_speed_math():
    lane,clock,timer,calls,_=service()
    lane.poll_once()
    adapter=lane.provider.primary
    base=adapter._request
    def advance(symbols):
        rows,errors=base(symbols)
        return {s:replace(q,price=q.price*1.01,quote_time=NOW+timedelta(minutes=5)) for s,q in rows.items()},errors
    adapter._request=advance
    clock[0]=NOW+timedelta(minutes=5);timer[0]=300
    p=lane.poll_once()
    assert p['quotes']['600498.SH']['speed_5m']==1.0
    assert p['quotes']['600498.SH']['quote_status']=='LIVE'


def test_watchlist_single_batch_and_dedup_across_subscribers():
    lane,clock,timer,calls,_=service()
    lane.subscribe('second','600498.SH',['600498.SH','000001.SZ'])
    lane.poll_once()
    assert len(calls)==1 and calls[0].count('sh600498')==1
    assert 'sz000001' in calls[0]
    lane.unsubscribe('view')
    assert '600498.SH' in lane.symbols()


def test_real_local_sse_fast_provenance_and_no_full_poll(monkeypatch):
    import json
    from urllib.request import urlopen
    from types import SimpleNamespace
    from src.streaming_ui import StreamingUI
    from src.market_data import fast_quote_service
    lane,clock,timer,calls,_=service()
    monkeypatch.setattr(fast_quote_service,'request_provider',lambda:lane.provider)
    ui=StreamingUI(SimpleNamespace(provider=SimpleNamespace(hybrid=True)),clock=lambda:NOW)
    try:
        url=ui.url.replace('index.html','fast_events')+'?current=600498.SH&enabled=1'
        with urlopen(url,timeout=3) as response:
            for line in response:
                if line.startswith(b'data:'):
                    payload=json.loads(line[5:]);break
        assert payload['channel']=='fast'
        assert payload['target']['source']=='tencent'
        assert all(k in payload['target'] for k in ('symbol','quote_time','trade_date','quote_status','received_at'))
        assert ui.bus.latest is None  # Fast subscription never activates full market polling.
    finally:ui.close()


def test_symbol_churn_reclaims_cache_and_history():
    lane,clock,timer,calls,_=service()
    adapter=lane.provider.primary
    original=adapter._request
    def selected(symbols):
        q=original(['600498.SH'])[0]['600498.SH']
        return {s:replace(q,symbol=s) for s in symbols},[]
    adapter._request=selected
    for i in range(100):
        lane.subscribe('view',f'{600000+i:06d}.SH')
        timer[0]=i*2;lane.poll_once()
    assert len(lane.quotes)==len(lane.samples)==len(lane.buffer._index)==1
    assert len(adapter._previous_raw)==1
