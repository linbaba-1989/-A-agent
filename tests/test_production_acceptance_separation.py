"""Run the real app entry point and pages with offline market/model boundaries."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src.market_clock import to_beijing
from src.market_data_router import MarketDataSelection
from src.market_cache import history_indicators

ROOT = Path(__file__).resolve().parents[1]
CLOSED = datetime(2026, 9, 22, 19)
STAMP = to_beijing(datetime(2026, 9, 22, 15)).timestamp()
FORBIDDEN = ('PREFLIGHT_BLOCKED', 'PRECHECK_BLOCKED', 'MARKET_NOT_OPEN',
             'CONTINUOUS_TIME_LT_10_MIN', 'Required Source', 'provider_init_count', 'worker_count')


class Provider:
    provider_name = 'XtDataCenter Token'
    universe_audit = None
    def get_stock_universe(self):
        return ['600498.SH', '600000.SH', '000001.SZ']

    def get_full_ticks(self, symbols):
        return {s:dict(time=STAMP*1000,lastPrice=43.48,lastClose=43.0,volume=10000,pvolume=1000000,
                       amount=43_480_000,high=44,low=42,open=43) for s in symbols}

    def _valid_tick(self, tick, require_time=False): return True
    def tick_timestamp(self, tick): return tick['time']/1000
    def normalized_instrument(self, symbol):
        return {'name':'测试股票','float_volume':100_000_000,'total_volume':200_000_000}
    def get_instrument_detail(self, symbol):
        return {'InstrumentName':'测试股票','FloatVolume':100_000_000,'TotalVolume':200_000_000}
    def normalize_tick(self, symbol, tick): return {'timestamp':'2026-09-22T15:00:00'}
    def get_local_history(self, symbols, count): return {s:history_frame() for s in symbols}
    def close(self): pass


def history_frame():
    dates=pd.date_range('2026-01-01',periods=170,freq='B')
    return pd.DataFrame({'time':dates.astype('int64')//1_000_000,'open':40.,'high':44.,'low':39.,
                         'close':41.,'volume':10000.,'amount':410000.,'suspendFlag':0},
                        index=[int(d.strftime('%Y%m%d')) for d in dates])


class Scanner:
    def __init__(self, provider):
        self.provider=provider;self.scan_calls=0;self.instrument_cache={}
        self.snapshot_history=SimpleNamespace(speed=lambda *_:'unavailable')
        self.history_service=SimpleNamespace(
            info=lambda:SimpleNamespace(status='ready',ready=3,total=3,insufficient=0,unavailable=0,failed=0),
            ensure=lambda *_:history_frame(),indicators=lambda symbol,now:history_indicators(history_frame(),now))
    @staticmethod
    def _volume_ratio(*_): return 1.0
    def scan(self, limit, progress_callback=None):
        self.scan_calls+=1
        return SimpleNamespace(rows=[{'symbol':'600498.SH','name':'测试股票','lastPrice':43.48,
                                      'change_pct':1.1163,'amount':43_480_000,'turnover_rate':1.0}],
                               diagnostics=SimpleNamespace(elapsed_seconds=.01))


@pytest.fixture
def app_factory(monkeypatch):
    import src.market_clock as clock
    import src.market_data_router as router
    import src.scanner as scanner
    import src.agent as agent
    import src.realtime_market as realtime
    import ui.pages.realtime_market as realtime_page
    import ui.pages.stock_research as research_page
    import ui.pages.watchlist as watchlist_page
    import ui.components.acceptance as panel

    class Router:
        def __init__(self, **_): pass
        def select(self, *, acceptance_source=None, preferred_source='auto'):
            self.selection=MarketDataSelection(Provider(),'XtDataCenter Token','connected',False,
                qmt_fallback_status='disabled',required_source='XtDataCenter Token' if acceptance_source else None,
                fallback_enabled=False,token_configured=True)
            return self.selection
        def close(self): pass

    st.cache_resource.clear()
    monkeypatch.setenv('A_AGENT_ACCEPTANCE_SOURCE','xtdatacenter')  # legacy launch must be harmless
    monkeypatch.delenv('A_AGENT_ACCEPTANCE_MODE',raising=False)
    monkeypatch.delenv('A_AGENT_STREAM_DIAGNOSTICS',raising=False)
    for module in (clock,realtime_page,research_page,watchlist_page,panel):
        monkeypatch.setattr(module,'beijing_now',lambda:CLOSED)
    monkeypatch.setattr(panel,'worker_count',lambda:1)
    monkeypatch.setattr(router,'MarketDataRouter',Router)
    monkeypatch.setattr(scanner,'MarketScanner',Scanner)
    monkeypatch.setattr(agent,'StockResearchAgent',lambda:SimpleNamespace(router=SimpleNamespace(role_states={})))
    original_feed=realtime.RealtimeMarketFeed
    monkeypatch.setattr(realtime,'RealtimeMarketFeed',lambda p,s:original_feed(p,s,snapshot_cache=None))

    def create(page, mode=False):
        app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=20)
        app.session_state['nav_page']=page
        app.session_state['acceptance_mode']=mode
        return app.run()
    yield create
    st.cache_resource.clear()


def all_text(app):
    return '\n'.join(str(x.value) for kind in ('markdown','caption','info','warning','error','title')
                     for x in getattr(app,kind))


@pytest.mark.parametrize('page', ['总览','实时行情','全A扫描','自选股','个股研究','AI研究院','策略回测','设置'])
def test_closed_normal_mode_all_real_pages_render_without_preflight(app_factory,page):
    app=app_factory(page)
    assert not app.exception
    assert app.radio(key='main_navigation').value.endswith(page)
    assert not any(word in all_text(app) for word in FORBIDDEN)
    assert not any(b.label in ('Start Acceptance','停止验收') for b in app.button)
    assert app.session_state['realtime_quote_status']=='CACHED'


def test_closed_scanner_button_executes_and_displays_after_hours_basis(app_factory):
    app=app_factory('全A扫描')
    start=next(b for b in app.button if b.label=='开始扫描')
    assert not start.disabled
    start.click().run()
    assert not app.exception
    assert app.session_state['scan_rows'][0]['symbol']=='600498.SH'
    assert '盘后模式 / 使用最近有效收盘快照' in all_text(app)


def test_closed_ai_button_runs_with_closed_quote_context(app_factory,monkeypatch):
    import ui.pages.stock_research as page
    received=[]
    def run(ctx,symbol,mode,facts):
        received.append(facts)
        return {'symbol':symbol,'employees':{},'chief_researcher':{'success':True,'data':{}}}
    monkeypatch.setattr(page,'_run_research',run)
    monkeypatch.setattr(page,'render_ai_report',lambda *_:None)
    monkeypatch.setattr(page,'_render_records',lambda *_:None)
    app=app_factory('个股研究')
    start=next(b for b in app.button if b.label=='开始AI研究')
    assert not start.disabled
    start.click().run()
    assert not app.exception and received
    assert received[0]['market_status']=='closed'
    assert received[0]['quote_type']=='latest_available_snapshot'
    assert received[0]['quote_time']=='2026-09-22T15:00:00'
    assert received[0]['source']=='XtDataCenter Token'


def test_closed_realtime_renders_cached_price(app_factory):
    app=app_factory('实时行情')
    assert '43.48' in all_text(app) and 'CACHED' in all_text(app)
    assert '显示最近有效行情' in all_text(app)


@pytest.mark.parametrize('page',['实时行情','全A扫描','个股研究','设置'])
def test_closed_acceptance_only_disables_start_and_keeps_page(app_factory,page):
    app=app_factory(page,True)
    assert not app.exception
    assert any(b.label=='Start Acceptance' and b.disabled for b in app.button)
    assert any(t.value==page for t in app.title)
    assert '当前已收盘' in all_text(app)
    assert not app.error
