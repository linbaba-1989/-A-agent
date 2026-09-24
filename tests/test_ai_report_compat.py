"""Offline regression coverage for saved research report schema variants."""
import json
from contextlib import nullcontext
from pathlib import Path

from ui.components import ai_report
from ui.research_view import adapt_research_result, chief_conclusions
from ui.view_models import report_view


class RecordingStreamlit:
    def __init__(self):
        self.output = []

    def markdown(self, value, **_kwargs):
        self.output.append(str(value))

    def write(self, value):
        self.output.append(str(value))

    def caption(self, value):
        self.output.append(str(value))

    def json(self, value):
        self.output.append(str(value))

    def error(self, value):
        self.output.append(str(value))

    def expander(self, *_args, **_kwargs):
        return nullcontext()

    def tabs(self, labels):
        return [nullcontext() for _ in labels]


def render_offline(monkeypatch, result):
    recorder = RecordingStreamlit()
    monkeypatch.setattr(ai_report, "st", recorder)
    # A UI render must never start a new study or reach a Provider.
    from src.agent import StockResearchAgent
    monkeypatch.setattr(StockResearchAgent, "analyze", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("UI render invoked a real research run")))
    ai_report.render_ai_report(result)
    return "\n".join(recorder.output)


def test_new_chief_schema_prefers_formal_fields(monkeypatch):
    result = {"chief_researcher": {"success": True, "data": {
        "overall_view": "bearish", "short_term_view": "neutral_bearish", "short_term": "bullish",
        "mid_term_view": "neutral_bullish", "mid_term": "bearish", "trend_state": "uptrend",
        "risk_level": "high", "confidence": 50, "confidence_cap": 65,
        "evidence_quality": "medium", "data_completeness": "partial",
        "summary": "有条件的研究结论", "evidence": []}}}
    view = report_view(result)
    assert dict(ai_report.headline_values(view)) == {
        "总体观点": "偏空", "短线观点": "中性偏空", "中期观点": "中性偏多",
        "趋势状态": "上升趋势", "风险等级": "高", "置信度": "50 / 65",
        "数据完整度": "部分缺失"}
    assert view["chief"]["evidence_quality"] == "medium"
    assert "有条件的研究结论" in render_offline(monkeypatch, result)


def test_legacy_chief_uses_only_present_legacy_fields(monkeypatch):
    result = {"chief_researcher": {"success": True, "data": {
        "short_term": "bearish", "mid_term": "bullish", "trend": "downtrend",
        "risk": "medium", "confidence": 50, "final_summary": "历史报告说明"}}}
    view = report_view(result)
    values = dict(ai_report.headline_values(view))
    assert values["总体观点"] == "--"
    assert values["短线观点"] == "偏空"
    assert values["中期观点"] == "偏多"
    assert values["趋势状态"] == "下降趋势"
    assert values["风险等级"] == "中等"
    assert values["数据完整度"] == "--"
    assert "历史报告说明" in render_offline(monkeypatch, result)


def test_missing_horizons_and_optional_lists_show_placeholders(monkeypatch):
    result = {"chief_researcher": {"success": True, "data": {"confidence": 50}}}
    view = report_view(result)
    values = dict(ai_report.headline_values(view))
    assert values["短线观点"] == values["中期观点"] == "--"
    assert values["趋势状态"] == values["风险等级"] == "--"
    assert values["置信度"] == "50 / --"
    adapted = adapt_research_result(result)
    for key in ("key_drivers", "key_risks", "key_conflicts", "invalidation_conditions",
                "missing_evidence", "evidence"):
        assert adapted["chief_researcher"]["data"][key] == []
    assert all(not values for values in chief_conclusions(result).values())
    assert "AI 综合研判" in render_offline(monkeypatch, result)


def test_all_five_roles_accept_partial_rows_without_model_calls(monkeypatch):
    result = {"fact_data": {}, "employees": {
        "technical_analyst": {"success": True, "data": {"summary": "技术说明"}},
        "fundamental_event_analyst": {"success": True, "data": None},
        "sentiment_analyst": {"success": True},
        "risk_officer": None},
        "chief_researcher": {"success": True, "data": {"summary": "综合说明"}}}
    output = render_offline(monkeypatch, result)
    assert "AI 综合研判" in output
    assert "综合说明" in output
    assert "--" in output


def test_saved_fixture_renders_offline_without_research_run(monkeypatch):
    fixture = Path(__file__).resolve().parents[1] / "ui" / "fixtures" / "research_report_preview.json"
    result = json.loads(fixture.read_text(encoding="utf-8"))
    output = render_offline(monkeypatch, result)
    assert result["example_preview"] is True
    assert "AI 综合研判" in output
    assert "短线观点" in output


def test_completed_history_record_shape_renders_without_research_run(monkeypatch):
    """Exercise the reported timestamp/latency shape using the saved UI fixture."""
    fixture = Path(__file__).resolve().parents[1] / "ui" / "fixtures" / "research_report_preview.json"
    saved = json.loads(fixture.read_text(encoding="utf-8"))
    saved.update({"created_at": "2026-09-24 00:44:05", "elapsed_seconds": 383.81})
    saved["chief_researcher"]["data"].update({"overall_view": "bearish", "confidence": 50})
    assert saved["created_at"] == "2026-09-24 00:44:05"
    assert saved["elapsed_seconds"] == 383.81
    view = report_view(saved)
    assert dict(ai_report.headline_values(view))["总体观点"] == "偏空"
    assert dict(ai_report.headline_values(view))["置信度"] == "50 / 65"
    assert "AI 综合研判" in render_offline(monkeypatch, saved)
