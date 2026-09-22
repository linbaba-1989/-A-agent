from ui.research_view import (chief_summary, data_status, elapsed_display, execution_state,
                              model_display, workforce_data)


def facts():
    return {"last_price": 10, "recent_daily_k": [{"close": 10}], "ma5": 9, "ma10": 9,
            "ma20": 8, "ma60": 8, "atr14": 1, "fundamental_data": "unavailable",
            "event_data": "unavailable", "announcement_data": "unavailable", "news_data": "unavailable",
            "industry_data": "unavailable", "sentiment_external_data": "unavailable",
            "volume_ratio": 1.2, "turnover_rate": 2, "speed_1m": "unavailable",
            "speed_3m": "unavailable", "speed_5m": "unavailable", "security_status": "normal"}


def test_workforce_cards_have_one_stable_role_set_and_data_status():
    result = {"fact_data": facts(), "employees": {
        "technical_analyst": {"success": True, "data": {}},
        "fundamental_event_analyst": {"success": True, "data": {"data_status": "unavailable"}},
        "sentiment_analyst": {"success": True, "data": {"data_status": "partial"}},
        "risk_officer": {"success": True, "data": {}},
    }, "chief_researcher": {"success": True, "data": {"data_gaps": ["news"]}}}
    statuses = workforce_data(result)
    assert statuses["technical_analyst"] == "available"
    assert statuses["fundamental_event_analyst"] == "unavailable"
    assert statuses["sentiment_analyst"] == "partial"
    assert statuses["chief_researcher"] == "partial"


def test_state_and_elapsed_are_user_facing_stable():
    assert execution_state("idle", {}) == "等待"
    assert execution_state("working", {}) == "运行中"
    assert execution_state("working", {"success": True}) == "完成"
    assert execution_state("working", {"success": False, "error": "TimeoutError"}) == "超时"
    assert elapsed_display(66.666) == "66.7s"
    assert elapsed_display(None) == "--"


def test_models_and_modes_never_show_max_variant_or_infer_chief_fields():
    assert model_display("qwen3.8-max") == "Qwen 3.8 MAX"
    result = {"fact_data": facts(), "employees": {},
              "chief_researcher": {"success": True, "data": {"confidence": 40}}}
    summary = chief_summary(result)
    assert summary["stance"] is None
    assert summary["trend"] is None
    assert summary["risk"] is None
    assert summary["confidence"] == 40
