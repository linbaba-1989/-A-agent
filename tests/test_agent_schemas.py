import pytest
from pydantic import ValidationError

from src.agent_schemas import FundamentalEventReport, SentimentReport, TechnicalReport


def test_fundamental_unavailable_is_valid_structured_output():
    report = FundamentalEventReport(
        data_status="unavailable", confirmed_facts=[], available_fundamental_data=[], event_data=[],
        missing_data=["fundamental_data", "event_data"], possible_implications=[], confidence=0,
        summary="数据不足",
    )
    assert report.data_status == "unavailable"


def test_sentiment_unavailable_is_valid_structured_output():
    report = SentimentReport(
        data_status="unavailable", confirmed_market_signals=[], momentum_sentiment="unavailable", volume_sentiment="unavailable",
        crowding_risk="unavailable", missing_sentiment_data=["news", "social", "capital_flow"],
        confidence=0, summary="外部情绪数据不足",
    )
    assert report.missing_sentiment_data


def test_reports_forbid_extra_fields_and_wrong_shapes():
    payload = {
        "trend": {"direction": "up"}, "momentum": "x", "volume_price": "x",
        "moving_average_structure": "x", "support": {}, "resistance": [],
        "breakout_status": "x", "bullish_signals": [], "bearish_signals": [],
        "data_gaps": [], "confidence": 50, "summary": "x", "extra": True,
    }
    with pytest.raises(ValidationError):
        TechnicalReport.model_validate(payload)
