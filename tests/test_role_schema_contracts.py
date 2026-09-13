import pytest

from src.agent_schemas import (ChiefReport, FundamentalEventReport, RiskReport,
                               SentimentReport, TechnicalReport)


CASES = [
    (TechnicalReport, {"trend": "sideways", "momentum": "neutral", "volume_price": "unavailable",
        "moving_average_structure": "mixed", "support": [], "resistance": [],
        "breakout_status": "no_confirmed_breakout", "bullish_signals": [], "bearish_signals": [],
        "data_gaps": ["speed_1m"], "confidence": 50, "summary": "技术数据有限"}),
    (FundamentalEventReport, {"data_status": "unavailable", "confirmed_facts": [],
        "available_fundamental_data": [], "event_data": [],
        "missing_data": ["fundamental_data", "event_data"], "possible_implications": [],
        "confidence": 0, "summary": "数据不足"}),
    (SentimentReport, {"data_status": "unavailable", "confirmed_market_signals": [],
        "momentum_sentiment": "unavailable", "volume_sentiment": "unavailable",
        "crowding_risk": "unavailable", "missing_sentiment_data": ["external_sentiment"],
        "confidence": 0, "summary": "情绪数据不足"}),
    (RiskReport, {"technical_risks": [], "data_quality_risks": ["fundamental unavailable"],
        "positioning_risks": [], "bull_case_challenges": [], "invalid_assumptions": [],
        "missing_information": ["position"], "risk_level": "unknown", "confidence": 20,
        "summary": "风险数据有限"}),
    (ChiefReport, {"status": "degraded", "missing_roles": ["sentiment_analyst"],
        "confirmed_facts": [], "data_gaps": ["sentiment"], "bull_case": [], "bear_case": [],
        "key_catalysts": [], "key_risks": [], "technical_view": "neutral",
        "fundamental_view": "unavailable", "sentiment_view": "unavailable", "risk_view": "unknown",
        "points_of_agreement": [], "points_of_disagreement": [], "confidence": 20,
        "final_summary": "证据不足"}),
]


@pytest.mark.parametrize(("schema", "payload"), CASES)
def test_each_role_accepts_its_canonical_structured_output(schema, payload):
    parsed = schema.model_validate(payload)
    assert parsed.model_dump() == payload
