from ui.components.ai_report import _conclusions, _data_strip, _headline, headline_values
from ui.research_view import chief_conclusions, chief_summary
from ui.view_models import report_view


def test_chief_headline_uses_schema_fields_and_confidence_cap():
    result = {"fact_data": {}, "employees": {}, "chief_researcher": {"success": True, "data": {
        "overall_view": "neutral_bearish", "short_term_view": "bearish",
        "mid_term_view": "neutral_bullish", "trend_state": "strong_up",
        "risk_level": "medium", "confidence": 45, "confidence_cap": 65,
        "data_completeness": "partial"}}}
    view = report_view(result)
    assert dict(headline_values(view)) == {
        "总体观点": "中性偏空", "短线观点": "偏空", "中期观点": "中性偏多",
        "趋势状态": "强势上行", "风险等级": "中等", "置信度": "45 / 65",
        "数据完整度": "部分缺失"}
    assert "Chief Schema · P1.9.1" in _headline(view)


def test_missing_chief_fields_stay_blank_without_specialist_inference():
    result = {"employees": {"technical_analyst": {"success": True, "data": {"trend": "up"}},
                            "risk_officer": {"success": True, "data": {"risk_level": "high"}}},
              "chief_researcher": {"success": True, "data": {"confidence": 20}}}
    summary = chief_summary(result)
    assert summary["trend"] is None
    assert summary["risk"] is None
    assert summary["data_status"] is None
    assert dict(headline_values(report_view(result))) == {
        "总体观点": "--", "短线观点": "--", "中期观点": "--",
        "趋势状态": "--", "风险等级": "--", "置信度": "20 / --",
        "数据完整度": "--"}


def test_key_conclusions_are_schema_values_only_and_html_is_escaped():
    result = {"chief_researcher": {"data": {
        "key_drivers": ["MA20上方"], "key_risks": ["波动较高"],
        "key_conflicts": [{"topic": "horizon", "description": "短弱中强", "roles": ["technical"]}],
        "invalidation_conditions": [{"condition": "跌破MA20且放量", "timeframe": "daily",
                                     "evidence_source": "technical"}],
        "missing_evidence": ["公告 <待核实>"]}}}
    assert chief_conclusions(result) == {
        "关键驱动": ["MA20上方"], "关键风险": ["波动较高"], "关键冲突": ["短弱中强"],
        "失效条件": ["跌破MA20且放量"], "缺失证据": ["公告 <待核实>"]}
    html = _conclusions(result)
    assert "公告 &lt;待核实&gt;" in html
    assert "timeframe" not in html


def test_successful_model_does_not_turn_missing_facts_into_complete_data():
    result = {"fact_data": {"last_price": 10},
              "employees": {"fundamental_event_analyst": {"success": True,
                                                           "data": {"data_status": "unavailable"}}},
              "chief_researcher": {"success": True, "data": {"data_completeness": "partial"}}}
    view = report_view(result)
    assert view["data_levels"]["fundamental_event_analyst"] == "unavailable"
    assert "基本面/事件" in _data_strip(view)
    assert "不可用" in _data_strip(view)
    assert view["data_status"] == "部分缺失"
