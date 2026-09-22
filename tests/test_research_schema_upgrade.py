import pytest
from pydantic import ValidationError

from src.agent_schemas import (ChiefReport, InvalidationCondition, SentimentReport,
                               TechnicalReport, RESEARCH_TIME_HORIZONS)


def old_chief():
    return {"status": "degraded", "missing_roles": [], "confirmed_facts": [], "data_gaps": [],
            "bull_case": [], "bear_case": [], "key_catalysts": [], "key_risks": [],
            "technical_view": "unavailable", "fundamental_view": "unavailable",
            "sentiment_view": "unavailable", "risk_view": "unavailable", "points_of_agreement": [],
            "points_of_disagreement": [], "confidence": 20, "final_summary": "旧结果"}


def test_time_horizons_are_shared_and_explicit():
    assert RESEARCH_TIME_HORIZONS == {"short_term": "当前至未来1~5个交易日的交易结构",
                                      "mid_term": "约2~8周的趋势结构"}


def test_chief_accepts_short_mid_conflict_and_structured_invalidation():
    payload = old_chief() | {"overall_view": "neutral_bearish", "short_term_view": "neutral_bearish",
        "mid_term_view": "neutral_bullish", "trend_state": "strong_up", "risk_level": "medium",
        "confidence": 40, "confidence_cap": 65, "data_completeness": "partial",
        "key_conflicts": [{"topic": "time_horizon", "description": "短线转弱但中期未破坏",
                            "roles": ["technical", "risk"]}],
        "invalidation_conditions": [{"condition": "日线收盘跌破MA20且放量", "timeframe": "daily",
                                      "evidence_source": "technical"}]}
    parsed = ChiefReport.model_validate(payload)
    assert parsed.short_term_view == "neutral_bearish"
    assert parsed.mid_term_view == "neutral_bullish"
    assert isinstance(parsed.invalidation_conditions[0], InvalidationCondition)


def test_confidence_cannot_exceed_cap():
    with pytest.raises(ValidationError):
        ChiefReport.model_validate(old_chief() | {"confidence": 70, "confidence_cap": 65})


def test_sentiment_missing_market_breadth_can_be_unavailable():
    report = SentimentReport(data_status="unavailable", confirmed_market_signals=[],
                             momentum_sentiment="unavailable", volume_sentiment="unavailable",
                             crowding_risk="unavailable", missing_sentiment_data=["breadth"],
                             confidence=0, summary="数据不足", market_phase="unavailable")
    assert report.market_phase == "unavailable"


def test_technical_new_fields_are_structured_and_extra_is_rejected():
    payload = {"trend": "neutral", "momentum": "neutral", "volume_price": "unavailable",
               "moving_average_structure": "unavailable", "support": [], "resistance": [],
               "breakout_status": "unavailable", "bullish_signals": [], "bearish_signals": [],
               "data_gaps": [], "confidence": 40, "summary": "数据不足",
               "short_term_view": "neutral_bearish", "mid_term_view": "neutral_bullish",
               "trend_state": "strong_up", "confidence_cap": 65, "evidence_quality": "low"}
    assert TechnicalReport.model_validate(payload).trend_state == "strong_up"
    with pytest.raises(ValidationError):
        TechnicalReport.model_validate(payload | {"invented_field": True})


def test_old_chief_without_new_fields_remains_loadable_without_inference():
    parsed = ChiefReport.model_validate(old_chief())
    assert parsed.short_term_view == "unavailable"
    assert parsed.mid_term_view == "unavailable"
    assert parsed.trend_state == "unavailable"
