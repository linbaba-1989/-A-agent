import json

from src.agent import StockResearchAgent
from src.few_shot import FewShotRetriever, load_library
from src.llm_router import RouterResult


def selected(role, facts, specialists=None):
    return FewShotRetriever(enabled=True).retrieve(role, facts, specialists)[1]


def test_library_is_role_isolated_and_contains_legacy_plus_supplements():
    library = load_library()
    assert not library.errors
    assert {role: len(cases) for role, cases in library.cases.items()} == {
        "technical_analyst": 12, "fundamental_event_analyst": 8,
        "sentiment_analyst": 9, "risk_officer": 8, "chief_researcher": 9,
    }
    assert all(case.role == role for role, cases in library.cases.items() for case in cases)
    assert len({case.case_id for cases in library.cases.values() for case in cases}) == 46


def test_disabled_by_default_shape_has_no_prompt_content(monkeypatch):
    monkeypatch.delenv("A_SHARE_FEW_SHOT_ENABLED", raising=False)
    retriever = FewShotRetriever()
    text, audit = retriever.retrieve("technical_analyst", {"last_price": 10})
    assert not text
    assert audit["few_shot_status"] == "disabled"
    assert audit["selected_case_count"] == 0


def test_ten_golden_scenarios_are_deterministic_and_role_specific():
    cases = [
        ("technical_analyst", {"last_price": 10, "volume_ratio": 2, "high_20d": 9}, "technical-01"),
        ("technical_analyst", {"last_price": 10, "volume_ratio": .7, "change_pct": 1, "high_20d": 10}, "technical-10"),
        ("technical_analyst", {"last_price": 10, "ma5": 11, "ma10": 11, "ma20": 11, "ma60": 12,
                               "change_pct": 2}, "technical-12"),
        ("fundamental_event_analyst", {"fundamental_data": "unavailable", "valuation_data": "unavailable",
                                        "announcement_data": "unavailable", "news_data": "unavailable"}, "fundamental-08"),
        ("fundamental_event_analyst", {"announcement_data": {"status": "available", "text": "filing"}}, "fundamental-01"),
        ("sentiment_analyst", {}, "sentiment-09"),
        ("sentiment_analyst", {"market_breadth": 1, "limit_up_count": 20, "broken_board_rate": 1,
                                "sector_strength": 2, "leader_status": "strong", "market_phase": "climax"}, "sentiment-03"),
        ("risk_officer", {"last_price": 10, "atr14": 1, "turnover_rate": 2, "amount": 100}, "risk-03"),
        ("risk_officer", {"last_price": 10, "ma20": 11, "breakout_status": "unconfirmed"}, "risk-07"),
        ("chief_researcher", {"fundamental_data": "unavailable"}, "chief-03"),
    ]
    for role, facts, expected in cases:
        first = selected(role, facts)
        second = selected(role, json.loads(json.dumps(facts)))
        assert first["selected_case_ids"] == second["selected_case_ids"]
        assert expected in first["selected_case_ids"], (role, expected, first)
        assert first["estimated_few_shot_tokens"] <= 1000
        assert first["selected_case_count"] <= 3


def test_chief_can_use_specialist_conflict_without_mutating_facts():
    facts = {"last_price": 10, "fundamental_data": "unavailable"}
    specialists = {
        "technical_analyst": {"success": True, "data": {"short_term_view": "bearish", "mid_term_view": "bullish"}},
        "risk_officer": {"success": True, "data": {"short_term_view": "neutral_bearish"}},
    }
    audit = selected("chief_researcher", facts, specialists)
    assert "chief-09" in audit["selected_case_ids"]
    assert facts == {"last_price": 10, "fundamental_data": "unavailable"}


def test_prompt_injection_is_optional_and_audited():
    class CaptureRouter:
        def __init__(self):
            self.messages = None

        def call(self, role, messages, schema, analysis_id, analysis_mode):
            self.messages = messages
            return RouterResult(role, "mock", "mock", True, {"summary": "ok"}, 0.0)

    facts = {"last_price": 10, "ma5": 8, "ma10": 8, "ma20": 8, "ma60": 12}
    router = CaptureRouter()
    agent = StockResearchAgent(router, few_shot_retriever=FewShotRetriever(enabled=True))
    agent._role_call("technical_analyst", facts, "a1", "standard")
    assert "RELEVANT_CASES" in router.messages[0]["content"]
    assert agent._few_shot_audit["technical_analyst"]["few_shot_status"] == "selected"

    off_router = CaptureRouter()
    off_agent = StockResearchAgent(off_router, few_shot_retriever=FewShotRetriever(enabled=False))
    off_agent._role_call("technical_analyst", facts, "a2", "standard")
    assert "RELEVANT_CASES" not in off_router.messages[0]["content"]
    assert off_agent._few_shot_audit["technical_analyst"]["few_shot_status"] == "disabled"


def test_analyze_passes_structured_specialist_context_to_chief_retrieval():
    class ConflictRouter:
        def call(self, role, messages, schema, analysis_id, analysis_mode):
            if role == "chief_researcher":
                data = {"final_summary": "ok"}
            elif role == "technical_analyst":
                data = {"short_term_view": "bearish", "mid_term_view": "bullish"}
            else:
                data = {"short_term_view": "neutral_bearish"}
            return RouterResult(role, "mock", "mock", True, data, 0.0)

    agent = StockResearchAgent(ConflictRouter(), few_shot_retriever=FewShotRetriever(enabled=True))
    result = agent.analyze("600498.SH", {"last_price": 10, "fundamental_data": "unavailable"})
    assert "chief-09" in result["few_shot_audit"]["chief_researcher"]["selected_case_ids"]
