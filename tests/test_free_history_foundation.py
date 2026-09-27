"""Offline only: synthetic fixture values, injectable SDK transport and clocks."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from src.market_clock import BEIJING_TZ, DEFAULT_TRADING_CALENDAR
from src.market_data.contracts import MarketSnapshot
from src.market_data.historical import (HistoricalBar, HistoryResult, completed_date,
                                       validate, moving_averages, daily_volume_comparison)
from src.market_data.adapters.history_parsers import parse_rows
from src.market_data.adapters.history import AKShareHistoryProvider, BaoStockHistoryProvider
from src.market_data.history_cache import FreeHistoryCache
from src.market_data.free_history_provider import FreeHistoryProvider, create_free_history_provider

NOW = datetime(2026,9,24,19,tzinfo=BEIJING_TZ)
START, END = date(2026,9,22), date(2026,9,24)
SYMBOL = "600498.SH"
FIXTURES = Path(__file__).parent / "fixtures/free_history"


def parsed(flavor):
    rows = json.loads((FIXTURES / (flavor + ".json")).read_text(encoding="utf-8"))
    source = "baostock" if flavor == "baostock" else "akshare"
    return parse_rows(rows, SYMBOL, source, "raw", flavor)


def bar(day=START, source="akshare", adjustment="raw", **kwargs):
    return HistoricalBar(SYMBOL, day, open=10, high=12, low=9, close=11,
                         volume_shares=12000, amount_cny=130000,
                         source=source, adjustment=adjustment, **kwargs)


def test_parser_units_and_null():
    em,_ = parsed("akshare_em")
    sina,_ = parsed("akshare_sina")
    bao,_ = parsed("baostock")
    assert em[0].volume_shares == sina[0].volume_shares == bao[0].volume_shares == 12000
    assert em[0].amount_cny == sina[0].amount_cny == bao[0].amount_cny == 130000
    assert em[1].volume_shares is None and bao[1].amount_cny is None
    assert sina[0].prev_close is None and sina[0].pct_change is None


@pytest.mark.parametrize("adj,flag",[("raw","3"),("qfq","2"),("hfq","1")])
def test_adjustment_mapping(adj, flag):
    rows=json.loads((FIXTURES/"baostock.json").read_text())
    rows[0]["adjustflag"]=flag
    bars,errors=parse_rows(rows[:1],SYMBOL,"baostock",adj,"baostock")
    assert bars[0].adjustment==adj and not errors
    rows[0]["adjustflag"]="wrong"
    bars,errors=parse_rows(rows[:1],SYMBOL,"baostock",adj,"baostock")
    assert not bars and errors


def test_partial_parse_isolation():
    rows=json.loads((FIXTURES/"baostock.json").read_text())
    rows.insert(0,{"date":"bad"})
    bars,errors=parse_rows(rows,SYMBOL,"baostock","raw","baostock")
    assert len(bars)==2 and len(errors)==1


def test_duplicates_and_order_deterministic():
    a,b=bar(),bar(START+timedelta(days=1))
    r=validate([b,a,replace(a,close=10)],SYMBOL,"akshare","raw",START,b.trade_date,NOW)
    assert [x.trade_date for x in r.bars]==[START,b.trade_date]
    assert r.bars[0].close==10 and r.quality_status=="VALID"
    assert "input_not_sorted" in r.warnings
    assert any("duplicate" in w for w in r.warnings)


@pytest.mark.parametrize("change",[{"open":20},{"close":1},{"volume_shares":-1},
    {"amount_cny":-1},{"close":None},{"close":float("inf")},{"close":float("nan")}])
def test_invalid_bars(change):
    r=validate([replace(bar(),**change)],SYMBOL,"akshare","raw",START,START,NOW)
    assert r.data_status=="UNAVAILABLE"


def test_missing_date_no_fill():
    r=validate([bar(),bar(END)],SYMBOL,"akshare","raw",START,END,NOW)
    assert len(r.bars)==2 and r.data_status=="PARTIAL"
    assert "missing_session:2026-09-23" in r.warnings


def test_future_and_unfinished():
    early=NOW.replace(hour=11)
    r=validate([bar(date(2026,9,23)),bar(END),bar(date(2026,9,28))],
               SYMBOL,"akshare","raw",START,date(2026,9,28),early)
    assert [b.trade_date for b in r.bars]==[date(2026,9,23)]
    assert any("unfinished" in w for w in r.warnings)
    assert any("future_date" in w for w in r.warnings)
    assert completed_date(early)==date(2026,9,23)
    assert completed_date(NOW)==END


def test_no_basis_or_source_mix():
    r=validate([bar(),bar(source="baostock")],SYMBOL,"akshare","raw",START,START,NOW)
    assert r.quality_status=="DEGRADED"


def test_bse_unsupported_before_network():
    p=BaoStockHistoryProvider(loader=lambda _:pytest.fail("no request"))
    r=p.daily("920002.BJ",START,END)
    assert p.supported_exchanges=={"SH","SZ"}
    assert r.quality_status=="UNSUPPORTED" and r.data_status=="UNAVAILABLE"


def test_index_adjustment_unsupported():
    p=AKShareHistoryProvider(loader=lambda _:pytest.fail("no request"))
    assert p.daily("000001.SH",START,END,"qfq","index").quality_status=="UNSUPPORTED"


def test_index_ambiguous_volume_null():
    rows=json.loads((FIXTURES/"akshare_sina.json").read_text())
    bars,_=parse_rows(rows,"000001.SH","akshare","raw","akshare_index_sina")
    assert bars[0].volume_shares is None


class Fake:
    def __init__(self,name="akshare", fail=False, multiplier=1):
        self.name,self.fail,self.multiplier=name,fail,multiplier
        self.endpoint="fixture"
        self.calls=[]
    def supports(self,symbol,kind):
        return self.name=="akshare" or not symbol.endswith(".BJ")
    def daily(self,symbol,start,end,adjustment,kind):
        self.calls.append((symbol,start,end,adjustment,kind))
        if self.fail:
            return HistoryResult(symbol,source=self.name,adjustment=adjustment,warnings=["fixture_failure"])
        rows=[]
        day=start
        while day<=end:
            if DEFAULT_TRADING_CALENDAR.is_trading_day(day):
                rows.append(replace(bar(day,self.name,adjustment),symbol=symbol,
                    close=11*self.multiplier,open=10*self.multiplier,
                    high=12*self.multiplier,low=9*self.multiplier))
            day+=timedelta(days=1)
        return validate(rows,symbol,self.name,adjustment,start,end,NOW+timedelta(days=30))


def service(tmp_path, primary=None, fallback=None, clock=lambda:NOW, timer=lambda:0):
    return FreeHistoryProvider(primary=primary or Fake(),fallback=fallback or Fake("baostock"),
                               cache=FreeHistoryCache(tmp_path),clock=clock,timer=timer)


def test_primary_then_cache_no_network(tmp_path):
    p=Fake()
    s=service(tmp_path,p)
    first=s.daily(SYMBOL,60)
    second=service(tmp_path,Fake(fail=True)).daily(SYMBOL,60)
    assert first.source=="akshare" and not first.fallback_used
    assert second.data_status=="CACHED" and second.quality_status=="VALID"
    assert len(first.bars)==len(second.bars)==60


def test_fallback_no_splice(tmp_path):
    s=service(tmp_path,Fake(fail=True))
    r=s.daily(SYMBOL,60,"qfq")
    assert r.fallback_used and r.source=="baostock"
    assert {b.adjustment for b in r.bars}=={"qfq"}
    assert {b.source for b in r.bars}=={"baostock"}


def test_bse_failure_not_global(tmp_path):
    fallback=Fake("baostock")
    s=service(tmp_path,Fake(fail=True),fallback)
    r=s.daily("920002.BJ",120)
    assert "BSE_HISTORY_DEGRADED" in r.warnings and not fallback.calls
    assert s.daily(SYMBOL,60).fallback_used


def test_incremental_cache(tmp_path):
    times=[NOW]
    p=Fake()
    s=service(tmp_path,p,clock=lambda:times[0])
    s.daily(SYMBOL,60)
    times[0]=NOW+timedelta(days=4)
    r=s.daily(SYMBOL,60)
    assert p.calls[-1][1]==END and r.bars[-1].trade_date==date(2026,9,28)
    assert len(p.calls)==2


def test_adjusted_rebase_refresh(tmp_path):
    times=[NOW]
    p=Fake()
    s=service(tmp_path,p,clock=lambda:times[0])
    s.daily(SYMBOL,60,"qfq")
    p.multiplier=2
    times[0]=NOW+timedelta(days=4)
    r=s.daily(SYMBOL,60,"qfq")
    assert len(p.calls)==3 and p.calls[-1][1] < END
    assert {b.close for b in r.bars}=={22}
    assert "adjusted_basis_changed_full_refresh" in r.warnings


def test_cache_adjustment_and_endpoint_isolation(tmp_path):
    s=service(tmp_path)
    s.daily(SYMBOL,60,"raw")
    r=s.daily(SYMBOL,60,"qfq",network=False)
    assert not r.bars


def test_failure_throttle(tmp_path):
    p=Fake(fail=True); f=Fake("baostock",fail=True)
    s=service(tmp_path,p,f)
    s.daily(SYMBOL);s.daily(SYMBOL)
    assert len(p.calls)==len(f.calls)==1


def test_cached_old_is_not_complete(tmp_path):
    s=service(tmp_path)
    s.daily(SYMBOL,60)
    stale=service(tmp_path,Fake(fail=True),Fake("baostock",fail=True),
                  clock=lambda:NOW+timedelta(days=4)).daily(SYMBOL,60)
    assert stale.data_status=="CACHED" and stale.quality_status=="DEGRADED"
    assert moving_averages(stale)["MA60"]=="unavailable"


def test_ma_sixty_minimum_and_dataframe_compatibility(tmp_path):
    r=service(tmp_path).daily(SYMBOL,60)
    assert moving_averages(r)=={f"MA{n}":11 for n in (5,10,20,60)}
    r.bars=r.bars[-59:]
    assert moving_averages(r)["MA60"]=="unavailable"
    frame=r.frame()
    assert {"close","volume","amount"}.issubset(frame.columns)
    assert frame.attrs["volume_unit"]=="shares"


def test_daily_volume_is_shares_not_volume_ratio():
    r=HistoryResult(SYMBOL,[bar(date(2026,9,23))],quality_status="VALID",data_status="COMPLETE")
    q=MarketSnapshot(SYMBOL,volume_shares=24000,quote_time=NOW,quote_status="LIVE")
    result=daily_volume_comparison(q,r,NOW)
    assert result["daily_volume_multiple"]==2 and "volume_ratio" not in result
    assert daily_volume_comparison(replace(q,quote_status="STALE"),r,NOW)["status"]=="unavailable"


def test_free_init_without_sdk_or_paid_config():
    code="""import os,sys
os.environ['A_AGENT_MARKET_DATA_MODE']='free'
from src.market_data.free_history_provider import create_free_history_provider
p=create_free_history_provider()
assert not any(x in sys.modules for x in ['akshare','baostock','xtquant','src.xtdc_provider','src.qmt_provider'])
"""
    env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ("TOKEN","API_KEY","QMT"))}
    subprocess.run([sys.executable,"-c",code],env=env,check=True,
                   cwd=Path(__file__).resolve().parents[1],timeout=20)
    with pytest.raises(ValueError):
        create_free_history_provider(environ={})


def test_minute_period_rejected(tmp_path):
    with pytest.raises(ValueError):
        service(tmp_path).get_history([SYMBOL],"1m",120)

def test_quality_failure_triggers_fallback(tmp_path):
    class Broken(Fake):
        def daily(self,*args):
            r=super().daily(*args)
            r.quality_status="DEGRADED"
            r.data_status="PARTIAL"
            return r
    r=service(tmp_path,Broken()).daily(SYMBOL,60)
    assert r.fallback_used and r.source=="baostock"


def test_empty_both_providers_never_success(tmp_path):
    r=service(tmp_path,Fake(fail=True),Fake("baostock",fail=True)).daily(SYMBOL,60)
    assert not r.bars and r.data_status=="UNAVAILABLE"
    assert moving_averages(r)["MA5"]=="unavailable"


def test_cache_corruption_is_not_trusted(tmp_path):
    s=service(tmp_path)
    s.daily(SYMBOL,60)
    path=next(tmp_path.glob("*.json"))
    payload=json.loads(path.read_text())
    payload["bars"][0]["source"]="wrong"
    path.write_text(json.dumps(payload))
    p=Fake()
    s=service(tmp_path,p)
    s.daily(SYMBOL,60)
    assert len(p.calls)==1


def test_insufficient_requested_count_still_allows_valid_short_ma(tmp_path):
    r=service(tmp_path).daily(SYMBOL,59)
    assert moving_averages(r)["MA5"]==11
    assert moving_averages(r)["MA60"]=="unavailable"


def test_current_day_clamped_before_close(tmp_path):
    p=Fake()
    r=service(tmp_path,p,clock=lambda:NOW.replace(hour=11)).daily(SYMBOL,60,end=END)
    assert p.calls[-1][2]==date(2026,9,23)
    assert r.bars[-1].trade_date==date(2026,9,23)


def test_provider_multi_symbol_isolation():
    def loader(task):
        if task["symbol"]=="000001.SZ":
            raise OSError("fixture")
        return {"rows":json.loads((FIXTURES/"akshare_sina.json").read_text()),
                "flavor":"akshare_sina"}
    p=AKShareHistoryProvider(loader=loader,clock=lambda:NOW)
    results=p.batch([SYMBOL,"000001.SZ"],START,date(2026,9,23))
    assert results[SYMBOL].data_status=="COMPLETE"
    assert results["000001.SZ"].data_status=="UNAVAILABLE"


@pytest.mark.parametrize("adjustment,flag",[("raw","3"),("qfq","2"),("hfq","1")])
def test_sdk_worker_baostock_exact_flags(monkeypatch,adjustment,flag):
    from types import SimpleNamespace
    from src.market_data.adapters.history_worker import fetch
    calls=[]
    class Rows:
        error_code="0"
        def next(self):return False
    sdk=SimpleNamespace(login=lambda:SimpleNamespace(error_code="0"),logout=lambda:None,
        query_history_k_data_plus=lambda *a,**k:(calls.append((a,k)) or Rows()))
    monkeypatch.setitem(sys.modules,"baostock",sdk)
    fetch(dict(provider="baostock",symbol=SYMBOL,start=str(START),end=str(END),
               adjustment=adjustment,kind="equity"))
    assert calls[0][0][0]=="sh.600498"
    assert calls[0][1]["adjustflag"]==flag


@pytest.mark.parametrize("adjustment",["raw","qfq","hfq"])
def test_sdk_worker_akshare_exact_flags(monkeypatch,adjustment):
    from types import SimpleNamespace
    import pandas as pd
    from src.market_data.adapters.history_worker import fetch
    calls=[]
    def daily(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()
    monkeypatch.setitem(sys.modules,"akshare",SimpleNamespace(stock_zh_a_daily=daily))
    fetch(dict(provider="akshare",symbol=SYMBOL,start=str(START),end=str(END),
               adjustment=adjustment,kind="equity",endpoint="sina"))
    assert calls[0]["symbol"]=="sh600498"
    assert calls[0]["adjust"]==("" if adjustment=="raw" else adjustment)


def test_legacy_history_service_ensure_compatibility(tmp_path):
    from src.history_service import HistoricalDataService
    legacy=HistoricalDataService(service(tmp_path))
    frame=legacy.ensure(SYMBOL,60)
    assert len(frame)==60 and frame.attrs["adjustment"]=="raw"
    assert legacy.indicators(SYMBOL,NOW+timedelta(days=1))["previous_ma60"]==11
