import json
from pathlib import Path

from src.a_share_evals import evaluate_output, hallucination_claims, load_scenarios, run_mock_eval
from src.a_share_factors import (build_role_factors, confidence_cap, risk_factors,
                                 sentiment_factors, technical_factors)
from src.knowledge_loader import ROLE_KNOWLEDGE, load_role_knowledge, structured_prompt
from src.research_memory import ResearchMemory


FACTS = {"last_price": 40.75, "change_pct": 1.54, "ma5": 40.23, "ma10": 40.16,
         "ma20": 40.20, "ma60": 46.79, "atr_pct": 6.01, "range_position_20d": 55.49,
         "turnover_rate": 8.15, "volume_ratio": 1.30, "breakout_status": "未确认突破",
         "fundamental_data": "unavailable", "event_data": "unavailable",
         "sentiment_external_data": "unavailable"}


def test_technical_does_not_conclude_from_one_indicator():
    result = technical_factors({"last_price": 10, "ma5": 9})
    assert result["trend_state"]["value"] == "unavailable"
    full = technical_factors(FACTS)
    assert full["trend_state"]["value"] != "strong_up"
    assert "price <= MA60" in full["trend_state"]["evidence"]


def test_sentiment_missing_market_data_cannot_claim_phase():
    result = sentiment_factors(FACTS)
    assert result["market_phase"] == {"value": "unavailable", "status": "unavailable", "evidence": []}


def test_risk_is_specific_and_chief_confidence_is_capped():
    result = risk_factors(FACTS)
    assert any("ATR" in item for item in result["risk_factors"])
    assert "注意市场风险" not in json.dumps(result, ensure_ascii=False)
    assert confidence_cap(FACTS) == 65


def test_chief_framework_requires_conflict_resolution_not_voting():
    text = load_role_knowledge("chief_researcher")
    assert "禁止按岗位票数投票" in text and "显式列出冲突" in text


def test_unsupported_claim_and_hallucination_guards():
    assert hallucination_claims({"summary": "主力吸筹，必涨"}) == ["主力吸筹", "必涨"]
    scored = evaluate_output({"asserted_external_facts": ["未提供公告"], "evidence": [], "data_gaps": []}, {})
    assert scored["fact_grounding"] is False and scored["hallucination_count"] == 1


def test_terminology_and_evidence_policy_are_role_scoped():
    sentiment = load_role_knowledge("sentiment_analyst")
    fundamental = load_role_knowledge("fundamental_event_analyst")
    assert "龙头" in sentiment and "禁止滥用" in sentiment
    assert "公司公告" in fundamental and "MODEL_INFERENCE" in fundamental
    assert "technical_framework.md" in ROLE_KNOWLEDGE["technical_analyst"]
    assert "sentiment_framework.md" not in ROLE_KNOWLEDGE["technical_analyst"]


def test_prompt_has_all_required_sections():
    prompt = structured_prompt("技术分析员", "框架")
    for section in ("ROLE", "OBJECTIVE", "A_SHARE_FRAMEWORK", "AVAILABLE_FACTS", "EVIDENCE_RULES",
                    "FORBIDDEN_INFERENCES", "DECISION_PROCESS", "OUTPUT_SCHEMA"):
        assert section in prompt


def test_research_memory_schema_starts_pending_without_fake_outcomes():
    memory = ResearchMemory(analysis_id="a", symbol="600498.SH", timestamp="2026-09-14T10:00:00",
                            fact_bundle_hash="abc", role_outputs={}, chief_output={})
    assert memory.outcome_status == "pending"
    assert memory.forward_return_1d is None and memory.forward_return_5d is None


def test_cases_and_twenty_mock_eval_scenarios():
    root = Path(__file__).parents[1]
    for role in ("technical", "sentiment", "fundamental", "risk", "chief"):
        rows = [json.loads(line) for line in (root / "knowledge" / "cases" / f"{role}_cases.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 8
        assert all(set(row) == {"facts", "correct_analysis", "wrong_analysis", "error_reason"} for row in rows)
    assert len(load_scenarios()) == 20
    result = run_mock_eval()
    assert result["scenario_count"] == result["passed"] == 20


def test_600498_factor_bundle_exposes_role_layers():
    result = build_role_factors(FACTS)
    assert set(result) == {"technical_factors", "sentiment_factors", "risk_factors",
                           "fundamental_data_status", "confidence_cap"}
    assert result["confidence_cap"] == 65
