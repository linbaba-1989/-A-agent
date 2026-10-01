from streamlit.testing.v1 import AppTest

def test_unknown_market_ui_does_not_invent_scores():
    source='''
from unittest.mock import patch
from ui.pages import market_regime
with patch.object(market_regime,'load_current',return_value=dict(regime='UNKNOWN',as_of=None,trend_score=None,short_score=None,risk_score=None)):
    market_regime.render()
'''
    app=AppTest.from_string(source).run(timeout=20)
    assert not app.exception
    assert [m.value for m in app.metric]==['UNKNOWN']*4

def test_market_strength_does_not_create_a_stock_pattern():
    source='''
from unittest.mock import patch
from ui.pages import behavior_lab,market_regime
p={'data_end_date':'2026-09-30T15:00:00','current_match':{'pattern_id':None,'match_status':'OUT_OF_DISTRIBUTION'},
   'patterns':[],'strategy_candidates':[],'backtest':{'metrics':{'trades':0},'trades':[]}}
with patch.object(behavior_lab,'load_profile',return_value=p), patch.object(market_regime,'load_current',return_value={'regime':'TREND_STRONG','as_of':'2026-09-30T15:01:00'}):
    behavior_lab.render()
'''
    app=AppTest.from_string(source).run(timeout=20)
    assert not app.exception
    assert app.metric[0].value=='未匹配'
    assert any('TREND_STRONG' in x.value for x in app.markdown)
    assert any('NO_STATISTICALLY_USEFUL_PATTERN' in x.value for x in app.info)
