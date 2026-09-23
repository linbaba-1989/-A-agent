"""Offline checks for the stock-page AI workspace; every model call is forbidden."""
from streamlit.testing.v1 import AppTest
import pytest

from src.agent_schemas import (ChiefReport, FundamentalEventReport, RiskReport,
                               SentimentReport, TechnicalReport)
from ui.components.ai_progress import _role_model_display
from ui.pages.stock_research import load_example_report


@pytest.fixture(autouse=True)
def restore_page_stubs():
    import ui.pages.stock_research as page
    original_facts, original_run = page._research_facts, page._run_research
    yield
    page._research_facts, page._run_research = original_facts, original_run


WORKSPACE = """
import streamlit as st
from types import SimpleNamespace
import ui.pages.stock_research as page
from ui.research_view import ROLES

def forbidden(*args, **kwargs):
    raise AssertionError('a model call is forbidden in UI preview')

facts = {'code': '600498.SH', 'name': '测试股票', 'quote_time': '2026-09-22T15:00:00',
         'last_price': 10, 'ma5': 9, 'ma10': 9, 'ma20': 9, 'ma60': 8,
         'volume_ratio': 2, 'high_20d': 9, 'fundamental_data': 'unavailable',
         'announcement_data': 'unavailable', 'news_data': 'unavailable'}
page._research_facts = lambda *_: facts.copy()
router = SimpleNamespace(call=forbidden, role_states={role: 'idle' for role in ROLES})
workforce = SimpleNamespace(router=router, few_shot=SimpleNamespace(enabled=DEFAULT_ON))
ctx = {'workforce': workforce, 'routes': {role: {'candidates': [{'model': 'test-model'}]} for role in ROLES},
       'status': {'market': '已收盘'}}
snapshot = SimpleNamespace(quote={'timestamp': facts['quote_time']}, facts=facts)
page._render_ai_workspace(ctx, '600498.SH', 'standard', snapshot)
"""


def _text(app):
    return "\n".join(str(item.value) for kind in ("caption", "markdown", "info")
                     for item in getattr(app, kind))


def test_case_enhancement_switch_defaults_off_and_can_show_on_without_model_call():
    app = AppTest.from_string(WORKSPACE.replace("DEFAULT_ON", "False")).run()
    assert not app.exception
    assert not app.toggle[0].value
    assert "当前研究：关闭" in _text(app)
    assert _text(app).count("案例增强：关闭") == 5
    app.toggle[0].set_value(True).run()
    assert not app.exception
    assert "当前研究：开启" in _text(app)
    assert _text(app).count("案例增强：开启") == 5
    assert "动态选择最多2～3个A股案例" in _text(app)


def test_case_enhancement_switch_honors_existing_enabled_default():
    app = AppTest.from_string(WORKSPACE.replace("DEFAULT_ON", "True")).run()
    assert not app.exception and app.toggle[0].value
    assert "当前研究：开启" in _text(app)


def test_sentiment_deployment_id_shows_public_model_name_without_hiding_fallback():
    assert _role_model_display("sentiment_analyst", "ep-20260913102258-jq7jn") == "Doubao"
    assert _role_model_display("sentiment_analyst", "kimi-k3", "kimi") == "Kimi K3"


def test_case_enhancement_selection_applies_to_one_research_only():
    stub = """page._research_facts = lambda *_: facts.copy()
def fake_run(ctx, symbol, mode, facts):
    assert ctx['few_shot_run_enabled'] is True
    return {'symbol': symbol, 'analysis_mode': mode, 'fact_data': facts,
            'employees': {}, 'chief_researcher': {'success': False, 'error': 'offline'}}
page._run_research = fake_run"""
    app = AppTest.from_string(WORKSPACE.replace("DEFAULT_ON", "False").replace(
        "page._research_facts = lambda *_: facts.copy()", stub)).run()
    app.toggle[0].set_value(True).run()
    next(button for button in app.button if button.label == "开始AI研究").click().run()
    assert not app.exception
    assert app.session_state["research_history"][0]["few_shot_requested"] is True
    assert app.session_state["ai_case_enhancement_reset_pending"] is True
    app.run()
    assert not app.exception
    assert app.toggle[0].value is False
    assert "当前研究：关闭" in _text(app)


def test_local_case_preview_and_static_report_never_call_model():
    app = AppTest.from_string(WORKSPACE.replace("DEFAULT_ON", "False")).run()
    next(button for button in app.button if button.label == "预览案例匹配").click().run()
    assert not app.exception
    assert "案例匹配预览" in _text(app)
    assert "仅供预览，不调用 AI 模型，不计费" in _text(app)
    assert "technical-01" in _text(app)
    assert "当前研究：关闭" in _text(app)
    next(button for button in app.button if button.label == "预览新版研究报告").click().run()
    assert not app.exception
    assert "示例预览" in _text(app)
    assert "不是当前股票的真实研究结果" in _text(app)


def test_static_report_data_matches_all_five_schemas():
    report = load_example_report()
    assert report["example_preview"] is True
    ChiefReport.model_validate(report["chief_researcher"]["data"])
    for role, schema in (("technical_analyst", TechnicalReport),
                         ("fundamental_event_analyst", FundamentalEventReport),
                         ("sentiment_analyst", SentimentReport),
                         ("risk_officer", RiskReport)):
        schema.model_validate(report["employees"][role]["data"])
