from copy import deepcopy
from pathlib import Path
import json
import pandas as pd
from streamlit.testing.v1 import AppTest
from ui.pages.behavior_lab import statistics_rows

def test_small_sample_rates_are_not_displayed_as_success_probabilities():
    sample=dict(sample_count=2,effective_samples=2,confidence="INSUFFICIENT",positive_rate=1.,
        mean_return=.1,median_return=.1,mfe=.2,mae=-.03,average_win=.1,average_loss=0.,
        profit_loss_ratio=None,expectancy=.1)
    rows=statistics_rows({"statistics":{"train":{"5":sample}}})
    assert rows[0]["positive_rate"] is None and rows[0]["independent_samples"]==2

def test_behavior_lab_renders_unavailable_without_requesting_network():
    source="""
from unittest.mock import patch
from ui.pages import behavior_lab
with patch.object(behavior_lab,"load_profile",return_value=None):
    behavior_lab.render()
"""
    app=AppTest.from_string(source).run(timeout=20)
    assert not app.exception
    assert "Behavior Lab" in app.title[0].value
    assert len(app.info)==1

def test_behavior_lab_renders_no_candidate_result():
    # The test fixture is deterministic and independent of the user's real DB.
    source="""
from unittest.mock import patch
from ui.pages import behavior_lab
p={"data_end_date":"2026-09-30T15:00:00",
"current_match":{"pattern_id":None,"similarity":0.0,"sample_count":0,"confidence":"INSUFFICIENT","historical_matches":[]},
"patterns":[],"strategy_candidates":[],"backtest":{"metrics":{"trades":0,"win_rate":None,"total_return":0.0},"trades":[]},
"limitations":[]}
with patch.object(behavior_lab,"load_profile",return_value=p):
    behavior_lab.render()
"""
    app=AppTest.from_string(source).run(timeout=20)
    assert not app.exception
    assert any("NO_STATISTICALLY_USEFUL_PATTERN" in x.value for x in app.info)
    assert app.metric[2].value=="0"
