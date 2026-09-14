import json
from pathlib import Path
from types import SimpleNamespace

from src.model_arena import ArenaScore, reliability, role_leaderboard, score_result, stable_fact_bundle_hash
from src.model_registry import ModelRegistry, ProviderConfig
from src.provider_adapters import GLMAdapter, HunyuanAdapter, MiniMaxAdapter, StepFunAdapter
from ui.pages.ai_research import arena_table


def base_score(model: str, role: str = "technical_analyst", **changes):
    values = dict(provider="test", model_id=model, role=role, fact_bundle_hash="same",
                  schema_pass=True, fact_grounding=True, hallucination_count=0,
                  a_share_logic_score=3, sentiment_logic_score=3, risk_specificity_score=3,
                  evidence_quality_score=3, data_gap_awareness_score=3,
                  chief_conflict_handling_score=3, confidence_discipline_score=5,
                  score_reason="ok", hard_fail=False, hard_fail_reasons=())
    values.update(changes)
    return ArenaScore(**values)


def test_same_fact_bundle_has_stable_hash_and_golden_case_is_fixed():
    facts = {"code": "600498.SH", "price": 40.75}
    assert stable_fact_bundle_hash(facts) == stable_fact_bundle_hash(dict(reversed(list(facts.items()))))
    golden = json.loads(Path("evals/a_share/golden/600498_standard.json").read_text(encoding="utf-8"))
    assert golden["fact_bundle_hash"] == "674ea25673969caf7abd13d549208fb61bd310bcecd43ad6b3313d659f21ad4c"


def test_hallucination_and_confidence_violation_are_hard_failures():
    hallucination = score_result(provider="x", model_id="m", role="technical_analyst", fact_bundle_hash="h",
                                 report={"asserted_external_facts": ["编造订单"]})
    overconfident = score_result(provider="x", model_id="m", role="chief_researcher", fact_bundle_hash="h",
                                 report={"confidence": 66}, confidence_cap=65)
    assert hallucination.hard_fail and hallucination.overall_score == 0
    assert overconfident.hard_fail and "confidence_cap_violation" in overconfident.hard_fail_reasons


def test_role_specific_ranking_never_combines_roles():
    rows = [base_score("tech-good", a_share_logic_score=5),
            base_score("tech-low", a_share_logic_score=1),
            base_score("risk", role="risk_officer", risk_specificity_score=5)]
    ranked = role_leaderboard(rows, "technical_analyst")
    assert [row["model_id"] for row in ranked] == ["tech-good", "tech-low"]


def test_timeout_reliability_and_single_sample_has_no_fake_p95():
    stats = reliability([base_score("slow", timeout=True, latency=12.0, hard_fail=True,
                                    hard_fail_reasons=("timeout",))])
    assert stats["timeout_rate"] == 1 and stats["median_latency"] == 12.0
    assert stats["p95_latency"] is None


def test_unknown_cost_is_not_zero_and_ui_displays_unknown():
    row = base_score("m").public_dict()
    assert row["estimated_cost"] is None and row["cost_status"] == "unknown"
    assert arena_table([row], "technical_analyst")[0]["成本"] == "未知"


def test_candidate_adapters_share_interface_without_calling_api(monkeypatch):
    monkeypatch.delenv("CANDIDATE_KEY", raising=False)
    config = ProviderConfig("candidate", "model", None, "CANDIDATE_KEY", enabled=False,
                            production_enabled=False)
    for adapter_type in (GLMAdapter, HunyuanAdapter, MiniMaxAdapter, StepFunAdapter):
        adapter = adapter_type(config)
        assert adapter.health_check() == {"success": False, "error": "not_configured"}
        assert callable(adapter.chat_completion) and callable(adapter.structured_completion)


def test_provider_health_never_exposes_api_key(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "top-secret")
    monkeypatch.setenv("GLM_ENABLED", "true")
    registry = ModelRegistry()
    public = next(row for row in registry.statuses() if row["provider_name"] == "glm")
    assert "api_key" not in public and "top-secret" not in json.dumps(public)


def test_production_mapping_remains_unchanged():
    config = json.loads(Path("config/agent_config.json").read_text(encoding="utf-8"))["roles"]
    primary = {role: route["candidates"][0]["provider"] for role, route in config.items()}
    assert primary == {"technical_analyst": "deepseek", "fundamental_event_analyst": "qwen",
                       "sentiment_analyst": "doubao", "risk_officer": "kimi",
                       "chief_researcher": "deepseek"}
