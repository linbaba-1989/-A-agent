"""Offline P1.9.3F fact-relation checks; no provider or market access."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from evals.a_share.p193f_fact_relations import (
    derive_fact_relations,
    semantic_relation_violations,
)


def _facts(price=52.0, high=52.3, *, quote_type="synthetic_intraday_snapshot",
           market_status="open", source="synthetic_golden_fixture"):
    return {
        "fact_classification": "SYNTHETIC_GOLDEN_FIXTURE",
        "last_price": price, "high_10d": high,
        "quote_time": "2026-01-30T14:30:00+08:00",
        "quote_type": quote_type, "market_status": market_status,
        "source": source,
    }


@pytest.mark.parametrize("price,high,relation,breakout", [
    (52.0, 52.3, "below", False),
    (52.4, 52.3, "above", True),
    (52.3, 52.3, "equal", False),
])
def test_numeric_relations_are_pure_and_generic(price, high, relation, breakout):
    facts = _facts(price, high)
    original = deepcopy(facts)
    first = derive_fact_relations(facts)
    assert first == derive_fact_relations(deepcopy(facts))
    assert facts == original
    assert first["price_vs_10d_high"] == relation
    assert first["breakout_10d_high"] is breakout
    assert first["current_price"] == price
    assert first["ten_day_high"] == high
    assert first["source_context"] == "synthetic_eval_fixture"
    assert first["allowed_source_labels"] == ["synthetic_golden_fixture"]


@pytest.mark.parametrize("price,high,bad_claim,good_claim", [
    (52.0, 52.3, "现价52.0已突破52.3", "现价52.0尚未突破52.3"),
    (52.4, 52.3, "现价52.4低于10日高点52.3", "现价52.4高于10日高点52.3"),
    (52.3, 52.3, "现价52.3已突破10日高点", "现价52.3等于10日高点"),
])
def test_numeric_validator_detects_contradictions_for_below_above_and_equal(
    price, high, bad_claim, good_claim
):
    facts = _facts(price, high)
    bad = semantic_relation_violations({"summary": bad_claim}, facts)
    good = semantic_relation_violations({"summary": good_claim}, facts)
    assert bad["numeric_relation_violation"] is True
    assert bad["numeric_relation_violation_hits"][0]["path"] == "summary"
    assert good["numeric_relation_violation"] is False


def test_explicit_numeric_comparison_catches_inverted_relation():
    facts = _facts(52.0, 52.3)
    result = semantic_relation_violations({"summary": "52.0 > 52.3"}, facts)
    assert result["numeric_relation_violation"] is True
    assert semantic_relation_violations({"summary": "52.0 < 52.3"}, facts)[
        "numeric_relation_violation"
    ] is False


def test_breakout_of_shorter_window_does_not_imply_ten_day_breakout():
    facts = _facts(52.0, 52.3)
    assert semantic_relation_violations({
        "summary": "已突破5日高点51.8但未突破10日高点52.3"
    }, facts)["numeric_relation_violation"] is False
    assert semantic_relation_violations({
        "summary": "现价52.0已突破5日高点51.8与10日高点52.3"
    }, facts)["numeric_relation_violation"] is True


def test_intraday_snapshot_is_not_an_official_close():
    facts = _facts()
    relation = derive_fact_relations(facts)
    assert relation["snapshot_time"] == "14:30"
    assert relation["snapshot_type"] == "intraday"
    assert relation["is_market_close"] is False
    for text in ("今日收于52.0元", "今日收盘价52.0元", "日终确认上涨"):
        assert semantic_relation_violations({"summary": text}, facts)[
            "temporal_semantics_violation"
        ] is True
    for text in ("14:30盘中现价52.0", "尚未收盘，收盘数据不可用", "昨日收于51.0",
                 "若收盘跌破MA20，风险上升"):
        assert semantic_relation_violations({"summary": text}, facts)[
            "temporal_semantics_violation"
        ] is False


def test_explicit_official_close_allows_closing_language():
    facts = _facts(52.0, 52.3, quote_type="official_close", market_status="closed")
    relation = derive_fact_relations(facts)
    assert relation["snapshot_type"] == "official_close"
    assert relation["is_market_close"] is True
    assert semantic_relation_violations({"summary": "今日收于52.0元"}, facts)[
        "temporal_semantics_violation"
    ] is False


def test_missing_numeric_inputs_do_not_invent_a_relation():
    facts = _facts(price="unavailable")
    relation = derive_fact_relations(facts)
    assert relation["price_vs_10d_high"] == "unavailable"
    assert relation["breakout_10d_high"] is None
    assert semantic_relation_violations({"summary": "已突破10日高点"}, facts)[
        "numeric_relation_violation"
    ] is False


def test_synthetic_fixture_rejects_unprovided_external_attribution():
    facts = _facts()
    for report in (
        {"evidence": [{"source": "QMT/xtquant", "claim": "指标可用"}]},
        {"summary": "据Wind数据显示波动升高"},
        {"summary": "公司公告显示订单增长"},
        {"summary": "财联社报道称事件发生"},
    ):
        checked = semantic_relation_violations(report, facts)
        assert checked["provenance_violation"] is True
        assert checked["provenance_violation_hits"]


def test_known_provenance_is_allowed_and_internal_prompt_fields_are_not_external():
    facts = _facts(source="QMT/xtquant")
    facts.update({"volume_ratio": 0.85, "change_pct": 1.96})
    relation = derive_fact_relations(facts)
    assert "QMT" in relation["allowed_source_labels"]
    assert "QMT/xtquant" in relation["allowed_source_labels"]
    report = {"evidence": [
        {"source": "QMT", "claim": "现价52.0"},
        {"source": "program_factors", "claim": "ATR已提供"},
        {"source": "program_factors.technical_factors", "claim": "ATR已提供"},
        {"source": "confirmed_market_facts", "claim": "指标已提供"},
        {"source": "role_specific_data", "claim": "10日高点已提供"},
        {"source": "价格", "claim": "现价来自输入事实"},
        {"source": "volume_ratio与change_pct=1.96%", "claim": "量价指标已提供"},
    ]}
    assert semantic_relation_violations(report, facts)["provenance_violation"] is False
    assert semantic_relation_violations({"evidence": [{"source": "Wind"}]}, facts)[
        "provenance_violation"
    ] is True


def test_missing_source_discussion_is_not_an_attribution():
    facts = _facts()
    report = {"missing_information": ["缺失公司公告", "未提供Wind数据"]}
    assert semantic_relation_violations(report, facts)["provenance_violation"] is False


def test_actual_fixture_is_covered_without_case_id_special_case():
    path = Path(__file__).resolve().parents[1] / "evals" / "a_share" / "golden" / "p193" / "gc05_tech_bullish_risk_high.json"
    facts = json.loads(path.read_text(encoding="utf-8"))["facts"]
    relation = derive_fact_relations(facts)
    assert relation["current_price"] == 52.0
    assert relation["ten_day_high"] == 52.3
    assert relation["price_vs_10d_high"] == "below"
    assert relation["breakout_10d_high"] is False
    assert relation["snapshot_time"] == "14:30"
    assert relation["snapshot_type"] == "intraday"
    checked = semantic_relation_violations({
        "summary": "现价52.0已突破52.3，今日收于52.0元，QMT数据显示波动升高"
    }, facts)
    assert checked["numeric_relation_violation"] is True
    assert checked["temporal_semantics_violation"] is True
    assert checked["provenance_violation"] is True
