"""Offline public-format fixtures; never require network, QMT, or a user token."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
import json
import os
import subprocess
import sys
import pytest

from src.market_clock import BEIJING_TZ
from src.market_data.adapters.parsers import parse_sina, parse_tencent, to_wire, from_wire
from src.market_data.adapters.realtime import TencentRealtimeProvider, SinaRealtimeProvider
from src.market_data.contracts import MarketSnapshot, Security, SnapshotBatch, canonical_symbol
from src.market_data.factory import create_market_provider, market_data_mode
from src.market_data.free_provider import FreeMarketDataProvider
from src.market_data.quality import Freshness, Health
from src.market_data.universe import SecurityUniverse, board_for

FIXTURES = Path(__file__).parent / "fixtures" / "free_market"
OPEN = datetime(2026, 9, 24, 10, 0, 0, tzinfo=BEIJING_TZ)
CLOSED = datetime(2026, 9, 26, 11, 0, 0, tzinfo=BEIJING_TZ)


@pytest.fixture
def tx():
    return (FIXTURES / "tencent.txt").read_text(encoding="utf-8")


@pytest.fixture
def sina():
    return (FIXTURES / "sina.txt").read_text(encoding="utf-8")


def test_tencent_units_and_missing_fields(tx):
    rows, errors = parse_tencent(tx)
    q = rows["600498.SH"]
    assert q.price == 41.08 and q.prev_close == 42.54
    assert q.volume_shares == 41878700
    assert q.amount_cny == 1740050000
    assert q.total_market_cap == 18_000_000_000
    assert q.float_market_cap == 12_000_000_000
    assert q.turnover_rate == 3.29 and q.volume_ratio == 1.2
    assert q.quote_time.tzinfo is not None
    assert rows["000001.SZ"].turnover_rate is None
    assert rows["000001.SZ"].amount_cny is None
    assert rows["000001.SZ"].total_market_cap is None
    assert rows["000001.SZ"].volume_shares is None
    assert errors and "000636.SZ" not in rows


def test_sina_units_missing_fields_and_row_isolation(sina):
    rows, errors = parse_sina(sina)
    q = rows["600498.SH"]
    assert q.volume_shares == 41878716 and q.amount_cny == 1740050357
    assert q.change == pytest.approx(-1.46)
    assert q.pct_change == pytest.approx(-3.432064)
    assert q.turnover_rate is q.volume_ratio is q.total_market_cap is q.float_market_cap is None
    assert rows["000001.SZ"].amount_cny is None
    assert errors and "000636.SZ" not in rows


@pytest.mark.parametrize("symbol,wire", [
    ("600498.SH","sh600498"), ("000001.SZ","sz000001"),
    ("920002.BJ","bj920002"), ("430047.BJ","bj430047"), ("000001.SH","sh000001")])
def test_symbol_roundtrip(symbol,wire):
    assert to_wire(symbol) == wire
    assert from_wire(wire) == symbol


@pytest.mark.parametrize("symbol", ["sh600498","920002","600498.US","ABC.SH",""])
def test_internal_requires_explicit_exchange(symbol):
    with pytest.raises(ValueError):
        canonical_symbol(symbol)


def test_snapshot_missing_numeric_is_null():
    q = MarketSnapshot("600498.SH")
    assert q.to_dict()["volume_shares"] is None
    assert q.to_dict()["change"] is None
    with pytest.raises(ValueError):
        MarketSnapshot("600498.SH", quote_time=datetime(2026,1,1))


def test_freshness_all_states_and_session_reset():
    f = Freshness()
    q = MarketSnapshot("600498.SH",price=10,quote_time=OPEN,source="tencent")
    assert f.status(q,OPEN)[0] == "CACHED"
    q = replace(q,quote_time=OPEN+timedelta(seconds=2))
    assert f.status(q,OPEN+timedelta(seconds=2))[0] == "LIVE"
    assert f.status(q,OPEN+timedelta(seconds=3))[0] == "STALE"
    assert f.status(replace(q,price=None),OPEN)[0] == "UNAVAILABLE"
    assert f.status(replace(q,quote_time=OPEN+timedelta(hours=2)),OPEN)[0] == "UNAVAILABLE"
    assert f.status(q,CLOSED)[0] == "CACHED"
    assert f.status(replace(q,quote_time=OPEN-timedelta(days=1)),CLOSED)[0] == "STALE"
    lunch = OPEN.replace(hour=11,minute=40)
    assert f.status(q,lunch)[0] == "CACHED"
    afternoon = OPEN.replace(hour=13,minute=0,second=1)
    assert f.status(replace(q,quote_time=afternoon),afternoon)[0] == "CACHED"


def test_http_success_without_advance_is_not_live(tx):
    p = TencentRealtimeProvider(transport=lambda *a,**k:tx, clock=lambda:OPEN)
    first = p.snapshot(["600498.SH"])
    second = p.snapshot(["600498.SH"])
    assert first.snapshots["600498.SH"].quote_status == "CACHED"
    assert second.snapshots["600498.SH"].quote_status == "STALE"
    assert second.timestamp_advanced is False


def test_zero_price_not_whole_provider_failure(sina):
    p = SinaRealtimeProvider(transport=lambda *a,**k:sina,clock=lambda:CLOSED)
    b = p.snapshot(["600498.SH","000001.SZ","000823.SZ","000636.SZ"])
    assert b.zero_price_symbols == ["000823.SZ"]
    assert b.snapshots["000823.SZ"].price is None
    assert b.snapshots["000823.SZ"].quote_status == "UNAVAILABLE"
    assert "600498.SH" in b.valid_symbols
    assert b.coverage_ratio == .75 and b.valid_price_ratio == pytest.approx(2/3)
    assert b.snapshots["000636.SZ"].price is None
    assert b.errors


def good_batch():
    q = MarketSnapshot("600498.SH",price=10,quote_time=OPEN,quote_status="LIVE")
    return SnapshotBatch({q.symbol:q},[q.symbol],[q.symbol],[q.symbol],[],[],1,1,.1,
                         "fixture",timestamp_advanced=True,market_session="open")


def test_health_degradation_and_recovery_hysteresis():
    h=Health()
    good=good_batch()
    for _ in range(3): h.observe(good)
    assert h.state=="HEALTHY"
    bad=replace(good,coverage_ratio=.4)
    h.observe(bad)
    assert h.state=="HEALTHY" and h.consecutive_failures==1
    h.observe(bad)
    assert h.state=="DEGRADED"
    h.observe(bad)
    assert h.state=="UNAVAILABLE"
    h.observe(good); h.observe(good)
    assert h.state=="UNAVAILABLE"
    h.observe(good)
    assert h.state=="HEALTHY"
    assert h.consecutive_failures==0


@pytest.mark.parametrize("kwargs", [
    {"latency":20}, {"valid_price_ratio":.1}, {"timestamp_advanced":False},
    {"coverage_ratio":.2}, {"valid_symbols":[]}])
def test_health_inputs(kwargs):
    h=Health()
    for _ in range(3): h.observe(replace(good_batch(),**kwargs))
    assert h.state=="UNAVAILABLE"


def test_few_zero_prices_are_healthy():
    h=Health()
    for _ in range(3):h.observe(replace(good_batch(),valid_price_ratio=5563/5569))
    assert h.state=="HEALTHY"


class FakeProvider:
    def __init__(self,good=True):
        self.health=Health()
        self.good=good
        self.calls=0
    def snapshot(self,symbols):
        self.calls+=1
        b=good_batch()
        if not self.good:b=replace(b,coverage_ratio=0,valid_symbols=[])
        self.health.observe(b)
        return b


def test_fallback_threshold_and_recovery_cooldown():
    primary,fallback=FakeProvider(False),FakeProvider()
    clock=[0]
    p=FreeMarketDataProvider(primary=primary,fallback=fallback,timer=lambda:clock[0])
    for _ in range(2):p.snapshot(["600498.SH"])
    assert p.active_source=="tencent" and fallback.calls==0
    p.snapshot(["600498.SH"])
    assert p.active_source=="sina"
    primary.good=True
    for t in [29,30,60]:
        clock[0]=t;p.snapshot(["600498.SH"])
        assert p.active_source=="sina"
    clock[0]=90;p.snapshot(["600498.SH"])
    assert p.active_source=="tencent"


def test_bad_fallback_never_promoted():
    p=FreeMarketDataProvider(primary=FakeProvider(False),fallback=FakeProvider(False))
    for _ in range(4):p.snapshot(["600498.SH"])
    assert p.active_source=="tencent"


def test_universe_cached_across_requests_and_processes(tmp_path):
    calls=[]
    clock=[CLOSED]
    def loader():
        calls.append(1)
        return [Security("920002.BJ","fixture","BJ","BSE")]
    path=tmp_path/"universe.json"
    u=SecurityUniverse(path,loader,lambda:clock[0])
    assert u.get()[0].board=="BSE"
    u.get();u.get()
    assert len(calls)==1
    assert SecurityUniverse(path,loader,lambda:clock[0]).get()==u.get()
    assert len(calls)==1
    clock[0]+=timedelta(days=1)
    u.get()
    assert len(calls)==2
    payload=json.loads(path.read_text())
    assert set(payload["securities"][0])=={"symbol","name","exchange","board"}


def test_universe_failure_throttled_and_expired(tmp_path):
    calls=[]
    def fail():calls.append(1);raise OSError("fixture")
    u=SecurityUniverse(tmp_path/"none.json",fail,lambda:CLOSED)
    for _ in range(2):
        with pytest.raises(RuntimeError):u.get()
    assert len(calls)==1


@pytest.mark.parametrize("mode,expected",[("auto","auto"),("xtdc","xtdatacenter"),("qmt","qmt")])
def test_foundation_delegates_legacy_modes(mode,expected):
    calls=[]
    provider=object()
    class Router:
        def select(self,**kwargs):calls.append(kwargs);return SimpleNamespace(provider=provider)
    assert create_market_provider(mode=mode,router_factory=Router) is provider
    assert calls==[{"preferred_source":expected}]


def test_default_auto_and_invalid_mode():
    assert market_data_mode({})=="auto"
    with pytest.raises(ValueError):market_data_mode({"A_AGENT_MARKET_DATA_MODE":"bad"})


def test_free_factory_does_not_construct_router(monkeypatch):
    monkeypatch.delenv("XTDC_TOKEN",raising=False)
    def forbidden():raise AssertionError("legacy_called")
    assert isinstance(create_market_provider(mode="free",router_factory=forbidden),FreeMarketDataProvider)


def test_free_import_in_fresh_process_has_no_paid_modules():
    code = (
        "import sys; from src.market_data.factory import create_market_provider; "
        "p=create_market_provider(); "
        "assert p.provider_name=='FreeMarketDataProvider'; "
        "assert not any(x in sys.modules for x in ['src.qmt_provider','src.xtdc_provider','xtquant'])"
    )
    env={**os.environ,"A_AGENT_MARKET_DATA_MODE":"free"}
    env.pop("XTDC_TOKEN",None)
    subprocess.run([sys.executable,"-c",code],env=env,check=True,
                   cwd=Path(__file__).resolve().parents[1],timeout=20)


def test_batch_http_failure_isolated_and_access_denial_stops(tx):
    calls=[]
    def transport(url,**kwargs):
        calls.append(url)
        if len(calls)==1:raise HTTPError(url,429,"fixture",None,None)
        return tx
    p=TencentRealtimeProvider(transport=transport,clock=lambda:CLOSED)
    symbols=[f"{600000+i:06}.SH" for i in range(180)]
    b=p.snapshot(symbols)
    assert len(calls)<=2
    assert "HTTP_429" in b.errors
    assert len(b.requested_symbols)==180


def test_http_error_preserves_last_quote_as_stale(tx):
    p=TencentRealtimeProvider(transport=lambda *a,**k:tx,clock=lambda:OPEN)
    p.snapshot(["600498.SH"])
    def fail(*a,**k):raise TimeoutError()
    p.transport=fail
    b=p.snapshot(["600498.SH"])
    assert b.returned_symbols==[]
    assert b.snapshots["600498.SH"].price==41.08
    assert b.snapshots["600498.SH"].quote_status=="STALE"
    assert b.coverage_ratio==0


def test_canonical_api_rejects_vendor_notation():
    p=FreeMarketDataProvider(primary=FakeProvider(),fallback=FakeProvider())
    with pytest.raises(ValueError):p.quote("sh600498")


def test_transport_json_without_charset_uses_utf8(monkeypatch):
    from email.message import Message
    from src.market_data.adapters import public_http
    class Response:
        headers = Message()
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,count):return '{"name":"烽火通信"}'.encode('utf-8')
    monkeypatch.setattr(public_http,'urlopen',lambda *a,**k:Response())
    assert json.loads(public_http.get_text('https://example.test'))['name']=='烽火通信'


def test_dynamic_universe_paginates_and_normalizes():
    from src.market_data.adapters.public_http import tencent_universe
    calls=[]
    def transport(url):
        calls.append(url)
        start=0 if 'offset=0&' in url else 200
        rows=[{'code':f'sh{600000+i}', 'name':'fixture'} for i in range(start,min(start+200,201))]
        return json.dumps({'data':{'total':201,'rank_list':rows}})
    rows=tencent_universe(transport)
    assert len(calls)==2 and len(rows)==201
    assert rows[0].symbol=='600000.SH'
    assert rows[-1].exchange=='SH'


def test_universe_old_cache_has_expiry(tmp_path):
    path=tmp_path/'u.json'
    path.write_text(json.dumps({'version':1,'as_of':'2026-09-01','securities':[
        {'symbol':'600498.SH','name':'fixture','exchange':'SH','board':'MAIN'}]}))
    def fail():raise OSError()
    u=SecurityUniverse(path,fail,lambda:CLOSED)
    with pytest.raises(RuntimeError):u.get()


def test_full_snapshot_uses_injected_universe(tx,tmp_path):
    u=SecurityUniverse(tmp_path/'u.json',lambda:[Security('600498.SH','fixture','SH','MAIN')],lambda:CLOSED)
    p=TencentRealtimeProvider(universe=u,transport=lambda *a,**k:tx,clock=lambda:CLOSED)
    b=p.snapshot()
    assert b.requested_symbols==['600498.SH'] and b.coverage_ratio==1


def test_schema_rejects_nonfinite_numeric():
    for invalid in [float('nan'),float('inf'),True,'1']:
        with pytest.raises(ValueError):MarketSnapshot('600498.SH',price=invalid)


def test_snapshot_budget_and_missing_price():
    p=TencentRealtimeProvider(snapshot_budget=0,transport=lambda *a,**k:pytest.fail('unexpected HTTP'))
    b=p.snapshot(['600498.SH'])
    assert b.snapshots['600498.SH'].price is None
    assert 'snapshot_budget_exceeded' in b.errors


def test_stale_minority_cannot_make_mostly_stale_batch_healthy():
    h=Health()
    b=good_batch()
    b.snapshots={f'{600000+i}.SH':MarketSnapshot(f'{600000+i}.SH',price=10,
        quote_time=OPEN,quote_status='LIVE' if i==0 else 'STALE') for i in range(100)}
    b.returned_symbols=list(b.snapshots)
    for _ in range(3):h.observe(b)
    assert h.state=='UNAVAILABLE'


def test_original_router_ignores_new_opt_in_environment(monkeypatch):
    from src.market_data_router import MarketDataRouter
    monkeypatch.setenv('A_AGENT_MARKET_DATA_MODE','free')
    class Legacy:
        configured=True
        def check_connection(self):return SimpleNamespace(ok=True)
        def connection_diagnostics(self):return SimpleNamespace(connected=True)
        def close(self):pass
    token=Legacy()
    assert MarketDataRouter(lambda:token,Legacy).select().provider is token
