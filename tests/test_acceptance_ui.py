"""Offline Streamlit tests; no real Provider or market request is constructed."""
from streamlit.testing.v1 import AppTest
import pytest


@pytest.fixture(autouse=True)
def restore_panel_clock_and_process_reader(monkeypatch):
    import ui.components.acceptance as panel
    # The app snippets replace these with offline fixtures; restore afterward.
    monkeypatch.setattr(panel, 'beijing_now', panel.beijing_now)
    monkeypatch.setattr(panel, 'worker_count', panel.worker_count)


PANEL = '''
import streamlit as st
st.session_state.acceptance_mode = True
from datetime import datetime
from types import SimpleNamespace
from src.market_data_router import MarketDataSelection
import ui.components.acceptance as panel
panel.worker_count = lambda: 1
panel.beijing_now = lambda: datetime(2026,9,21,HOUR,MINUTE)
if 'owner' not in st.session_state:
    provider = object()
    selection = MarketDataSelection(provider,'XtDataCenter Token','connected',False,
        qmt_fallback_status='disabled', required_source='XtDataCenter Token',
        fallback_enabled=False,token_configured=True)
    feed = SimpleNamespace(provider=provider,provider_initializations=1)
    st.session_state.owner = SimpleNamespace(selection=selection,feed=feed,generation=1,
        stop_stream=lambda:None,verify_acceptance_connection=lambda:True)
owner=st.session_state.owner
st.toggle('实时刷新',key='realtime_enabled')
st.radio('频率',[1,2,5],key='realtime_frequency')
ctx={'source_resources':owner,'selection':owner.selection,'realtime_feed':owner.feed}
panel.render_acceptance_panel(ctx,'实时行情')
'''


def test_start_button_disabled_after_close_and_source_badge_explicit():
    app = AppTest.from_string(PANEL.replace('HOUR','16').replace('MINUTE','0')).run()
    assert not app.exception
    assert app.button[0].label == 'Start Acceptance' and app.button[0].disabled
    text = ' '.join(item.value for item in app.markdown)
    assert 'Required Source: XtDataCenter Token' in text
    assert 'Active Source: XtDataCenter Token' in text
    assert 'Fallback: DISABLED' in text
    assert not app.error
    assert any('当前已收盘' in x.value for x in app.info)


def test_start_callback_safely_sets_refresh_and_rerun_retains_grant():
    app = AppTest.from_string(PANEL.replace('HOUR','10').replace('MINUTE','0')).run()
    assert not app.button[0].disabled
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state['realtime_enabled'] is True
    assert app.session_state['realtime_frequency'] == 2
    assert app.session_state['acceptance_run']['generation'] == 1
    app.run()
    assert not app.exception
    assert app.button[0].disabled  # already authorized, no second start


def test_wrong_active_source_blocks_start_even_during_open():
    script = PANEL.replace('HOUR','10').replace('MINUTE','0').replace(
        "owner=st.session_state.owner", "owner=st.session_state.owner\nowner.selection.name='QMT Local'")
    app = AppTest.from_string(script).run()
    assert not app.exception
    assert app.button[0].disabled
    assert not app.error
    assert 'ACTIVE_SOURCE_MISMATCH' in ' '.join(x.value for x in app.caption)


def test_mode_toggle_survives_widget_cleanup_and_restores_source_choice(monkeypatch):
    monkeypatch.delenv('A_AGENT_ACCEPTANCE_MODE', raising=False)
    monkeypatch.setenv('A_AGENT_ACCEPTANCE_SOURCE', 'xtdatacenter')
    app = AppTest.from_string("""
import streamlit as st
from types import SimpleNamespace
from src.acceptance_source import source_request
from ui.components.acceptance import render_source_controls, render_developer_tools
page=st.radio('page',['settings','other'])
request=source_request(st.session_state,{})
if page=='settings':
    render_source_controls({'source_request':request,'source_resources':SimpleNamespace(close=lambda:None)})
    render_developer_tools()
st.write('lock='+str(request.acceptance_source))
""").run()
    assert not app.selectbox[0].disabled
    app.selectbox[0].select('qmt').run()
    app.toggle[0].set_value(True).run()
    assert not app.exception
    assert app.session_state['market_source'] == 'qmt'
    assert app.session_state['acceptance_mode'] is True
    assert app.selectbox[0].disabled
    app.radio[0].set_value('other').run()
    app.run()
    assert 'lock=xtdatacenter' in ' '.join(x.value for x in app.markdown)
    app.radio[0].set_value('settings').run()
    app.toggle[0].set_value(False).run()
    assert not app.selectbox[0].disabled
    assert app.selectbox[0].value == 'qmt'
    assert 'lock=None' in ' '.join(x.value for x in app.markdown)


def test_normal_mode_never_runs_preflight_or_stops_production_stream():
    script = PANEL.replace('HOUR','16').replace('MINUTE','0').replace(
        'st.session_state.acceptance_mode = True', 'st.session_state.acceptance_mode = False')
    script = script.replace("panel.render_acceptance_panel(ctx,'实时行情')",
        "panel.worker_count = lambda: (_ for _ in ()).throw(AssertionError('preflight must not run'))\npanel.render_acceptance_panel(ctx,'实时行情')\nst.title('实时行情业务内容')")
    app=AppTest.from_string(script).run()
    assert not app.exception and not app.error and not app.button
    assert app.title[0].value == '实时行情业务内容'


def test_closed_acceptance_does_not_close_existing_beta_stream():
    script = PANEL.replace('HOUR','16').replace('MINUTE','0').replace(
        'stop_stream=lambda:None', "stop_stream=lambda: (_ for _ in ()).throw(AssertionError('must not stop Beta'))")
    app=AppTest.from_string(script).run()
    assert not app.exception
    assert app.button[0].disabled
