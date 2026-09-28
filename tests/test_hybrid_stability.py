from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from src.market_clock import BEIJING_TZ, DEFAULT_TRADING_CALENDAR
from src.market_data.contracts import MarketSnapshot, SnapshotBatch
from src.market_data.quality import Health
from src.market_data.free_provider import FreeMarketDataProvider
from src.market_data.adapters.realtime import SinaRealtimeProvider
from src.market_data.history_cache import FreeHistoryCache
from src.market_data.historical import HistoricalBar, validate
from src.market_data.hithink_history import HithinkHistoryProvider
from src.market_data.hithink_client import ApiResult
from src.market_data.hithink_reference import ReferenceCache
from src.market_data.hithink_realtime import HithinkRealtimeProvider
from src.market_data.sector_service import SectorService

NOW = datetime(2026,9,28,10,tzinfo=BEIJING_TZ)


def batch(stale=50):
    quotes = {f'{i:06}.SH':MarketSnapshot(f'{i:06}.SH',price=10,quote_time=NOW,
               quote_status='STALE' if i<stale else 'LIVE',source='fixture') for i in range(100)}
    return SnapshotBatch(quotes,list(quotes),list(quotes),list(quotes),[],[],1,1,1,'fixture',
                         timestamp_advanced=True,market_session='open')


def test_provider_health_not_symbol_freshness():
    b=batch();h=Health()
    b.provider_evidence=dict(active_symbols=50,active_advancing_symbols=50)
    for _ in range(3):h.observe(b)
    assert h.state=='HEALTHY'
    assert sum(q.quote_status=='STALE' for q in b.snapshots.values())==50


@pytest.mark.parametrize('change,reason', [({'coverage_ratio':.85},'COVERAGE_FAILURE'),
    ({'errors':['HTTP_503']},'HTTP_FAILURE'),({'errors':['TimeoutError']},'NETWORK_FAILURE'),
    ({'valid_price_ratio':.2},'VALID_PRICE_FAILURE'),({'timestamp_advanced':False},'TIMESTAMP_STALL'),
    ({'latency':20},'LATENCY')])
def test_real_provider_failure_still_triggers_fallback(change,reason):
    class Provider:
        def __init__(self,b):self.b=b;self.health=Health()
        def snapshot(self,symbols):self.health.observe(self.b);return self.b
    p=Provider(replace(batch(),**change));f=Provider(batch(0))
    free=FreeMarketDataProvider(primary=p,fallback=f)
    for _ in range(2):free.snapshot(['600498.SH']);assert free.active_source=='tencent'
    free.snapshot(['600498.SH'])
    assert free.active_source=='sina'
    assert reason in p.health.reason
    assert free.last_observations['tencent']['batch']['errors']==p.b.errors


def test_sina_partial_response_remains_degraded():
    provider=SinaRealtimeProvider(clock=lambda:NOW)
    symbols=[f'{i:06}.SH' for i in range(1600)]
    def request(group):
        if group[0]==symbols[0]:raise TimeoutError()
        return {s:MarketSnapshot(s,price=10,quote_time=NOW,source='sina') for s in group},[]
    provider._request=request
    for _ in range(3):result=provider.snapshot(symbols)
    assert result.coverage_ratio==.5 and result.coverage_status=='PARTIAL'
    assert result.provider_evidence['degraded']
    assert len(result.returned_symbols)==800
    assert provider.health.state=='UNAVAILABLE'


@pytest.mark.parametrize('symbol',['600984.SH','300052.SZ','688237.SH'])
def test_history_cache_reread_deterministic(symbol,tmp_path):
    calendar=SimpleNamespace(refresh=lambda:None,is_trading_day=DEFAULT_TRADING_CALENDAR.is_trading_day)
    day=NOW.date()-timedelta(days=1);days=[]
    while len(days)<121:
        if calendar.is_trading_day(day):days.append(day)
        day-=timedelta(days=1)
    days=sorted(days);days.pop(40)
    bars=[HistoricalBar(symbol,d,10,12,9,11,100,1100,source='hithink') for d in days]
    cache=FreeHistoryCache(tmp_path)
    def make():
        p=HithinkHistoryProvider(None,calendar,cache,clock=lambda:NOW)
        p.daily=lambda s,start,end,*args:validate(bars,s,'hithink','raw',start,end,NOW,calendar)
        return p
    p=make();first=p.load(symbol,120);second=p.load(symbol,120);third=make().load(symbol,120,network=False)
    assert [r.quality_status for r in (first,second,third)]==['DEGRADED']*3
    assert [len(r.bars) for r in (first,second,third)]==[120]*3
    assert first.warnings==second.warnings==third.warnings
    assert second.data_status==third.data_status=='CACHED'


def test_sector_resume_sync(tmp_path):
    cache=ReferenceCache(tmp_path,lambda:NOW);calls=[];failed=[False]
    catalogs={tag:dict(item=[dict(index_thscode=code,index_name=tag)]) for tag,code in
              [('industry','881101.TI'),('cn_concept','885431.TI')]}
    def catalog(tag):cache.write('catalog_'+tag,catalogs[tag]);return ApiResult('CACHED',data=catalogs[tag])
    def members(code,*args):
        calls.append(code)
        if code=='881101.TI' and not failed[0]:failed[0]=True;return ApiResult('NETWORK_ERROR')
        cache.write('members_'+code,{'item':[]});return ApiResult('SUCCESS',data={'item':[]})
    provider=SimpleNamespace(cache=cache,catalog=catalog,members=members)
    one=SectorService(provider)
    assert one.sync(max_requests=2)=='PARTIAL'
    two=SectorService(provider)  # Restart: reuse disk cache, retry only failed code.
    assert two.sync(max_requests=1)=='COMPLETE'
    assert calls==['881101.TI','885431.TI','881101.TI']
    assert two.memberships('600498.SH')['quality_status']=='COMPLETE'


def test_sector_missing_catalog_never_complete(tmp_path):
    cache=ReferenceCache(tmp_path,lambda:NOW)
    cache.write('catalog_industry',{'item':[{'index_thscode':'881101.TI'}]})
    cache.write('members_881101.TI',{'item':[]})
    assert SectorService(SimpleNamespace(cache=cache)).memberships('600498.SH')['quality_status']=='PARTIAL'


def test_sector_rate_limit_stops_run(tmp_path):
    cache=ReferenceCache(tmp_path,lambda:NOW);calls=[]
    provider=SimpleNamespace(cache=cache,catalog=lambda tag:ApiResult('SUCCESS',data={'item':[
        dict(index_thscode='881101.TI',index_name='one'),dict(index_thscode='881102.TI',index_name='two')]}),
        members=lambda *a:calls.append(a) or ApiResult('RATE_LIMITED'))
    assert SectorService(provider).sync()=='RATE_LIMITED'
    assert len(calls)==1


def test_index_page_timestamp_is_not_per_symbol_live():
    clock=[NOW];price=[3000]
    client=SimpleNamespace(get=lambda *a:ApiResult('SUCCESS',data=dict(timestamp=int(clock[0].timestamp()*1000),
        item=[dict(thscode='000001.SH',last_price=price[0],prev_price=2990,price_change_ratio_pct=.3)])))
    calendar=SimpleNamespace(refresh=lambda:None,is_trading_day=DEFAULT_TRADING_CALENDAR.is_trading_day)
    p=HithinkRealtimeProvider(client,SimpleNamespace(refresh=lambda:None,rows={}),calendar,clock=lambda:clock[0])
    p.snapshot(['000001.SH'],index=True)
    clock[0]+=timedelta(seconds=1);price[0]+=1
    r=p.snapshot(['000001.SH'],index=True);q=r.snapshots['000001.SH']
    assert q.quote_status=='CACHED' and q.quote_time is None
    proof=r.provider_evidence['index_observations']['000001.SH']
    assert proof['price_changed_since_previous_sample'] is True
    assert proof['availability']=='AVAILABLE_CURRENT_SESSION' and not proof['per_symbol_live_verified']
