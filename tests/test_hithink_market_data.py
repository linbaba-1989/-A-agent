"""Official REST contract regression. All HTTP is injected; no public network."""
from datetime import datetime,date,timedelta
from pathlib import Path
import json
from types import SimpleNamespace
from urllib.parse import urlparse,parse_qs
import pytest
from src.market_clock import BEIJING_TZ,market_session,DEFAULT_TRADING_CALENDAR
from src.market_data.hithink_client import HithinkRestClient,CODE_STATUS,ENDPOINTS,ApiResult,NoRedirect
from src.market_data.hithink_reference import (ReferenceCache,HithinkSecurityUniverseProvider,
    HithinkCalendar,HithinkSectorProvider,milliseconds,parse_ticker)
from src.market_data.hithink_realtime import HithinkRealtimeProvider,parse_snapshot,OfficialFreshness
from src.market_data.hithink_history import HithinkHistoryProvider,parse_bars,date_ms
from src.market_data.hithink_provider import HithinkOfficialProvider
from src.market_data.history_cache import FreeHistoryCache
from src.market_data.factory import create_market_provider,market_data_mode
from src.market_data.contracts import canonical_symbol
from src.market_data.historical import moving_averages

NOW=datetime(2026,9,24,10,tzinfo=BEIJING_TZ)
CLOSED=datetime(2026,9,27,10,tzinfo=BEIJING_TZ)
FIX=json.loads((Path(__file__).parent/"fixtures/hithink/contracts.json").read_text(encoding="utf-8"))
SECRET="synthetic-fixture-credential-only"


def envelope(items=None,code=0,**data):
    return json.dumps(dict(code=code,message="success" if code==0 else "fixture_error",
        request_id="fixture-id",data=None if code else dict(item=items or [],timestamp=int(NOW.timestamp()*1000),**data))).encode()


class Transport:
    def __init__(self,callback):
        self.calls=[]
        self.callback=callback
    def __call__(self,url,headers,timeout):
        assert headers["X-api-key"]==SECRET
        query=parse_qs(urlparse(url).query)
        path=urlparse(url).path
        self.calls.append((path,query))
        return self.callback(path,query)


def client(callback,clock=lambda:100):
    t=Transport(callback)
    return HithinkRestClient(api_key=SECRET,transport=t,timer=clock),t


@pytest.mark.parametrize("code,status",list(CODE_STATUS.items()))
def test_business_codes_on_http_200(code,status):
    c,t=client(lambda *a:(200,{},envelope(code=code)))
    r=c.get("snapshot",{"thscodes":"600498.SH"})
    assert r.status==status and r.ok==(code==0)
    assert r.http_status==200 and r.code==code


@pytest.mark.parametrize("http,body",[(429,b"not-json"),(429,envelope()),(200,envelope(code=4001))])
def test_rate_limit_and_cooldown(http,body):
    clock=[100]
    c,t=client(lambda *a:(http,{"Retry-After":"7"},body),lambda:clock[0])
    r=c.get("snapshot")
    assert r.status=="RATE_LIMITED" and r.retry_after==7
    assert c.get("snapshot").message=="backoff_active" and len(t.calls)==1
    clock[0]+=7
    c.get("snapshot")
    assert len(t.calls)==2


def test_upstream_timeout_not_mislabeled_rate_limit():
    c,t=client(lambda *a:(200,{},envelope(code=5002)))
    c.get("history")
    assert c.get("snapshot").status=="UPSTREAM_TIMEOUT"
    assert len(t.calls)==1


def test_http_failure_not_success():
    c,_=client(lambda *a:(503,{},envelope()))
    assert c.get("snapshot").status=="HTTP_ERROR"


@pytest.mark.parametrize("payload",[b"[]",b"invalid",b'{"code":false,"data":{"item":[]}}',b'{"code":0,"data":null}'])
def test_bad_envelope(payload):
    c,_=client(lambda *a:(200,{},payload))
    assert not c.get("snapshot").ok


def test_missing_key_initializes_but_no_network(tmp_path):
    c=HithinkRestClient(api_key="",transport=lambda *a:pytest.fail("network"))
    p=HithinkOfficialProvider(client=c,cache_path=tmp_path,clock=lambda:CLOSED)
    r=p.snapshot(["600498.SH"])
    assert r.capability_status=="AUTH_NOT_CONFIGURED"
    assert r.snapshots["600498.SH"].quote_status=="UNAVAILABLE"
    assert p.history.load("600498.SH").warnings[0]=="AUTH_NOT_CONFIGURED"


def test_secret_redaction_and_exception():
    raw={"code":2001,"message":SECRET+" "+SECRET[:8]+" "+SECRET[-8:],
         "request_id":SECRET,"data":None,"X-api-key":SECRET,"Authorization":"fixture"}
    c,_=client(lambda *a:(200,{},json.dumps(raw).encode()))
    r=c.get("tickers")
    text=json.dumps(c.events)+repr(r)
    assert SECRET not in text and SECRET[:8] not in text and SECRET[-8:] not in text
    assert "X-api-key" not in text and "Authorization" not in text
    def fail(*a):raise OSError(SECRET)
    c.transport=fail
    assert SECRET not in repr(c.get("tickers"))
    assert c.redact({"api_key_configured":True})=={"api_key_configured":True}


def test_no_redirect_and_https_only():
    assert NoRedirect().redirect_request(None,None,302,None,None,"https://example.test") is None
    with pytest.raises(ValueError):HithinkRestClient(api_key=SECRET,base_url="http://example.test")


def refs(tmp_path,callback,clock=lambda:CLOSED):
    c,t=client(callback)
    cache=ReferenceCache(tmp_path,clock)
    return c,t,cache


def test_ticker_paging_and_persistent_name_cache(tmp_path):
    def respond(path,q):
        offset=int(q["offset"][0])
        rows=[{**FIX["tickers"][0],"thscode":f"{600000+i:06}.SH","ticker":f"{600000+i:06}"}
              for i in range(offset,min(offset+1000,1001))]
        return 200,{},envelope(rows)
    c,t,cache=refs(tmp_path,respond)
    p=HithinkSecurityUniverseProvider(c,cache)
    assert p.refresh().ok and len(p.rows)==1001 and len(t.calls)==2
    assert p.get()[0].exchange=="SH" and len(t.calls)==2
    assert HithinkSecurityUniverseProvider(c,cache).refresh().status=="CACHED"
    assert len(t.calls)==2


def test_ticker_exchange_not_guessed():
    with pytest.raises(ValueError):parse_ticker({**FIX["tickers"][0],"exchange":"SZ"})


def test_ticker_partial_page_not_cached_as_complete(tmp_path):
    def respond(path,q):
        if q["offset"]==["1000"]:return 200,{},envelope(code=2003)
        rows=[{**FIX["tickers"][0],"thscode":f"{600000+i}.SH","ticker":str(600000+i)} for i in range(1000)]
        return 200,{},envelope(rows)
    c,t,cache=refs(tmp_path,respond)
    p=HithinkSecurityUniverseProvider(c,cache)
    assert p.refresh().status=="PERMISSION_DENIED" and not cache.read("tickers")


def test_snapshot_units_null_and_timestamp():
    q=parse_snapshot(FIX["snapshot"][0],int(NOW.timestamp()*1000),{"600498.SH":"fixture"})
    assert q.volume_shares==41878716 and q.amount_cny==1740050357.25
    assert q.name=="fixture" and q.quote_time==NOW
    assert all(getattr(q,k) is None for k in ("turnover_rate","volume_ratio","total_market_cap","float_market_cap"))
    assert parse_snapshot({"thscode":"600498.SH"},None,{}).price is None
    assert parse_snapshot({**FIX["snapshot"][0],"last_price":0},None,{}).price is None


def test_index_snapshot_uses_same_schema_without_equity_prefix():
    row={**FIX["snapshot"][0],"thscode":"881101.TI"}
    assert parse_snapshot(row,int(NOW.timestamp()*1000),{},index=True).symbol=="881101.TI"
    with pytest.raises(ValueError):canonical_symbol("881101.TI")


def realtime(c,now=lambda:NOW):
    u=SimpleNamespace(rows={},refresh=lambda:ApiResult("CACHED"))
    cal=SimpleNamespace(refresh=lambda:None,is_trading_day=DEFAULT_TRADING_CALENDAR.is_trading_day)
    return HithinkRealtimeProvider(c,u,cal,now)


def test_full_snapshot_paging_failure_isolation():
    def respond(path,q):
        offset=int(q["offset"][0])
        if offset==100:return 200,{},envelope(code=3002)
        return 200,{},envelope([{**FIX["snapshot"][0],"thscode":f"{600000+offset}.SH"}],total=201)
    c,t=client(respond)
    r=realtime(c).snapshot_all()
    assert r.page_count==3 and len(r.returned_symbols)==2
    assert r.capability_status=="PARTIAL" and r.total==201
    assert all("thscodes" not in q for _,q in t.calls)


def test_full_snapshot_rate_limit_stops_pages():
    def respond(path,q):
        if q["offset"]==["100"]:return 429,{},b"limit"
        return 200,{},envelope(FIX["snapshot"],total=300)
    c,t=client(respond)
    r=realtime(c).snapshot_all()
    assert len(t.calls)==2 and r.capability_status=="PARTIAL" and "RATE_LIMITED" in r.errors


def test_partial_parser_isolation():
    c,_=client(lambda *a:(200,{},envelope(FIX["snapshot"]+[{"thscode":"bad"}],total=2)))
    r=realtime(c).snapshot(["600498.SH","600000.SH"])
    assert len(r.valid_symbols)==1 and r.snapshots["600000.SH"].price is None
    assert r.capability_status=="PARTIAL"


def test_freshness_live_stale_cached_unavailable():
    q=parse_snapshot(FIX["snapshot"][0],int(NOW.timestamp()*1000),{})
    f=OfficialFreshness()
    assert f.status(q,NOW)[0]=="CACHED"
    assert f.status(q,NOW)[0]=="STALE"
    q2=parse_snapshot(FIX["snapshot"][0],int((NOW+timedelta(seconds=1)).timestamp()*1000),{})
    assert f.status(q2,NOW+timedelta(seconds=1))[0]=="LIVE"
    assert f.status(parse_snapshot(FIX["snapshot"][0],None,{}),NOW)[0]=="UNAVAILABLE"
    holiday=parse_snapshot(FIX["snapshot"][0],int(CLOSED.timestamp()*1000),{})
    assert f.status(holiday,CLOSED)[0]=="CACHED"


def test_historical_units_adjustment_timezone():
    bars,errors=parse_bars(FIX["history"],"600498.SH","raw")
    assert not errors and bars[0].trade_date==date(2026,9,24)
    assert bars[0].volume_shares==41878716 and bars[0].amount_cny==1740050357.25
    assert bars[0].prev_close is None and bars[0].pct_change is None
    assert milliseconds(date_ms(date(2026,9,24))).hour==0


@pytest.mark.parametrize("adjustment,official",[("raw","none"),("qfq","forward"),("hfq","backward")])
def test_history_adjustment_mapping(tmp_path,adjustment,official):
    c,t=client(lambda *a:(200,{},envelope(FIX["history"])))
    p=HithinkHistoryProvider(c,DEFAULT_TRADING_CALENDAR,FreeHistoryCache(tmp_path),clock=lambda:CLOSED)
    r=p.daily("600498.SH",date(2026,9,24),date(2026,9,24),adjustment)
    assert r.bars[0].adjustment==adjustment
    assert t.calls[0][1]["adjust"]==[official]
    assert t.calls[0][1]["interval"]==["1d"]


def test_index_history_no_adjust_and_no_minutes(tmp_path):
    c,t=client(lambda *a:(200,{},envelope(FIX["history"])))
    p=HithinkHistoryProvider(c,DEFAULT_TRADING_CALENDAR,FreeHistoryCache(tmp_path),clock=lambda:CLOSED)
    p.daily("000001.SH",date(2026,9,24),date(2026,9,24),kind="index")
    assert t.calls[0][0]==ENDPOINTS["index_history"] and "adjust" not in t.calls[0][1]
    with pytest.raises(ValueError):p.daily("600498.SH",date(2026,9,24),date(2026,9,24),interval="1m")
    assert len(t.calls)==1


def test_catalog_and_members_cached(tmp_path):
    c,t,cache=refs(tmp_path,lambda path,q:(200,{},envelope(FIX["members"] if path==ENDPOINTS["members"] else FIX["catalog"])))
    p=HithinkSectorProvider(c,cache)
    r=p.catalog("industry")
    assert r.data["item"][0]["index_thscode"]=="881101.TI"
    r=p.members("881101.TI","fixture-industry","industry")
    assert r.data["members"][0]["symbol"]=="600498.SH"
    assert p.catalog("industry").status==p.members("881101.TI").status=="CACHED"
    assert len(t.calls)==2


def test_official_calendar_market_session_consistency(tmp_path):
    rows=[{"date":"20260923","date_ms":date_ms(date(2026,9,23))},
          {"date":"20260924","date_ms":date_ms(date(2026,9,24))}]
    c,t,cache=refs(tmp_path,lambda *a:(200,{},envelope(rows)))
    cal=HithinkCalendar(c,cache,clock=lambda:CLOSED)
    assert cal.refresh().ok
    assert cal.is_trading_day(date(2026,9,24))
    for hour,minute,session in [(9,0,"pre_open"),(9,20,"auction"),(10,0,"open"),(12,0,"lunch_break"),(14,0,"open"),(15,1,"closed")]:
        assert market_session(NOW.replace(hour=hour,minute=minute),cal)==session
    assert market_session(CLOSED,cal)=="closed"
    cal.refresh()
    assert len(t.calls)==1


def test_permission_failure_does_not_poison_other_capability():
    c,t=client(lambda path,q:(200,{},envelope(code=2003 if path==ENDPOINTS["index_snapshot"] else 0)))
    assert c.get("index_snapshot").status=="PERMISSION_DENIED"
    assert c.get("snapshot").ok and len(t.calls)==2


def test_hithink_explicit_auto_unchanged(monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY",SECRET)
    assert market_data_mode({})=="auto"
    h=create_market_provider(mode="hithink")
    assert h.provider_name=="HithinkOfficialProvider" and not h.client.events
    assert create_market_provider(mode="free").provider_name=="FreeMarketDataProvider"
    marker=object()
    router=SimpleNamespace(select=lambda **kwargs:SimpleNamespace(provider=marker))
    assert create_market_provider(mode="auto",router_factory=lambda:router) is marker

def test_hithink_cache_incremental_and_rebase(tmp_path):
    clock=[NOW.replace(hour=19)]
    factor=[1]
    def respond(path,q):
        start=milliseconds(int(q["start"][0])).date()
        end=milliseconds(int(q["end"][0])).date()
        rows=[]
        day=start
        while day<=end:
            if DEFAULT_TRADING_CALENDAR.is_trading_day(day):
                rows.append(dict(date_ms=date_ms(day),open_price=10*factor[0],
                    high_price=12*factor[0],low_price=9*factor[0],close_price=11*factor[0],
                    volume=1000,turnover=11000))
            day+=timedelta(days=1)
        return 200,{},envelope(rows)
    c,t=client(respond)
    cal=SimpleNamespace(refresh=lambda:None,is_trading_day=DEFAULT_TRADING_CALENDAR.is_trading_day)
    p=HithinkHistoryProvider(c,cal,FreeHistoryCache(tmp_path),clock=lambda:clock[0])
    assert len(p.load("600498.SH",60,"qfq").bars)==60
    assert p.load("600498.SH",60,"qfq").data_status=="CACHED" and len(t.calls)==1
    clock[0]+=timedelta(days=4)
    factor[0]=2
    r=p.load("600498.SH",60,"qfq")
    assert int(t.calls[1][1]["start"][0])==date_ms(date(2026,9,24))
    assert int(t.calls[2][1]["start"][0])<date_ms(date(2026,9,24))
    assert len(t.calls)==3 and {b.close for b in r.bars}=={22}
    assert moving_averages(r)["MA60"]==22


def test_backoff_is_bounded_and_increases():
    tick=[100]
    c,t=client(lambda *a:(200,{},envelope(code=4001)),lambda:tick[0])
    delays=[]
    for _ in range(8):
        result=c.get("snapshot")
        delays.append(result.retry_after)
        tick[0]+=result.retry_after
    assert delays[:3]==[2,4,8] and max(delays)==60


def test_current_bar_cannot_be_a_complete_close(tmp_path):
    c,t=client(lambda *a:(200,{},envelope(FIX["history"])))
    p=HithinkHistoryProvider(c,DEFAULT_TRADING_CALENDAR,FreeHistoryCache(tmp_path),clock=lambda:NOW)
    r=p.daily("600498.SH",date(2026,9,24),date(2026,9,24))
    assert not r.bars and r.data_status=="UNAVAILABLE"
    assert any("unfinished_daily" in w for w in r.warnings)


def test_official_calendar_rejects_inconsistent_fields(tmp_path):
    c,t,cache=refs(tmp_path,lambda *a:(200,{},envelope([dict(date="20260923",date_ms=date_ms(date(2026,9,24)))])))
    assert HithinkCalendar(c,cache,clock=lambda:CLOSED).refresh().status=="PROTOCOL_ERROR"


def test_realtime_missing_timestamp_never_live():
    c,t=client(lambda *a:(200,{},json.dumps(dict(code=0,data=dict(item=FIX["snapshot"],timestamp=None))).encode()))
    r=realtime(c).snapshot(["600498.SH"])
    assert r.snapshots["600498.SH"].quote_status=="UNAVAILABLE"


def test_smoke_report_serializes_batch_time_and_redacts():
    from scripts.hithink_market_smoke import serialize_report
    c,_=client(lambda *a:(200,{},envelope(FIX["snapshot"])))
    batch=realtime(c).snapshot(["600498.SH"])
    report=json.loads(serialize_report(c,{"batch":batch.metrics(),"message":SECRET}))
    assert report["batch"]["received_at"]==NOW.isoformat()
    assert SECRET not in report["message"]
