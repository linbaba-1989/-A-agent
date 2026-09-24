"""P1.9.3F eval-only integration checks; provider transport is always local."""
import json
import socket
import sys
from dataclasses import asdict

import httpx2
import pytest
from openai import OpenAI

from evals.a_share.p193_role_ab import (
    FACT_RELATION_DISCIPLINE, RoleIsolatedRunner, load_fixture, main,
)
from evals.a_share.p193f_fact_relations import derive_fact_relations
from src.agent_schemas import RiskReport
from src.llm_router import load_role_config
from src.model_registry import ModelRegistry, ProviderConfig
from src.provider_adapters import OpenAICompatibleAdapter
from src.usage_tracker import UsageTracker


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("real_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _registry(monkeypatch):
    monkeypatch.setenv("P193F_OFFLINE_KEY", "offline-test-key")
    return ModelRegistry({
        "deepseek": ProviderConfig(
            "deepseek", "deepseek-v4-pro", "https://api.deepseek.com", "P193F_OFFLINE_KEY",
            supports_custom_temperature=False, supports_reasoning_effort=True,
            reasoning_levels=("low", "high", "max"), reasoning_mode="deepseek"),
        "qwen": ProviderConfig("qwen", "qwen3.8-max",
                               "https://dashscope.aliyuncs.com/compatible-mode/v1", "P193F_OFFLINE_KEY"),
        "kimi": ProviderConfig("kimi", "kimi-k3", "https://api.moonshot.cn/v1", "P193F_OFFLINE_KEY"),
        "doubao": ProviderConfig("doubao", "ep-20260913102258-jq7jn",
                                 "https://ark.cn-beijing.volces.com/api/v3", "P193F_OFFLINE_KEY"),
    })


def test_eval_technical_non_thinking_wire_and_production_high_unchanged(monkeypatch, tmp_path):
    registry = _registry(monkeypatch)
    before = asdict(registry.get("deepseek"))
    bodies = []
    clients = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def factory(provider):
        sdk = OpenAI(api_key="offline-test-key", base_url=provider.base_url,
                     timeout=provider.timeout, max_retries=0,
                     http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
        clients.append(sdk)
        return OpenAICompatibleAdapter(provider, client=sdk)

    runner = RoleIsolatedRunner(registry=registry, client_factory=factory,
                                tracker=UsageTracker(tmp_path / "usage.jsonl"))
    try:
        prepared = runner.prepare_request("technical_analyst", load_fixture("GC01"), False, "offline")
        assert prepared["reasoning_effort"] == "none"
        assert prepared["provider"].reasoning_mode is None
        assert prepared["max_output_tokens"] == 4096
        assert prepared["provider"].timeout == 90
        runner.run_role("technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
                        evaluation_run_id="offline", anonymous_arm="OFF",
                        dry_run=False, allow_network=True)
        assert bodies[0]["reasoning_effort"] == "none"
        assert "thinking" not in bodies[0]
        assert bodies[0]["response_format"] == {"type": "json_object"}
        assert bodies[0]["max_tokens"] == 4096
        assert load_role_config()["technical_analyst"]["candidates"][0]["reasoning"]["standard"] == "high"
        runner.router._request(runner._candidate("technical_analyst")[1], [], "high")
        assert bodies[1]["reasoning_effort"] == "high"
        assert bodies[1]["thinking"] == {"type": "enabled"}
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & bodies[1].keys()
        assert asdict(registry.get("deepseek")) == before
    finally:
        for sdk in clients:
            sdk.close()


def test_risk_only_receives_relations_and_qwen_prompt_fingerprint_is_frozen(monkeypatch, tmp_path):
    runner = RoleIsolatedRunner(registry=_registry(monkeypatch),
                                tracker=UsageTracker(tmp_path / "usage.jsonl"))
    fixture = load_fixture("GC05")
    risk = runner.prepare_request("risk_officer", fixture, False, "offline")
    payload = json.loads(risk["messages"][1]["content"].split("FACT DATA（只读）：", 1)[1])
    relations = derive_fact_relations(fixture["facts"])
    assert payload["derived_fact_relations"]["price_vs_10d_high"] == relations["price_vs_10d_high"]
    assert payload["derived_fact_relations"]["breakout_10d_high"] is False
    assert payload["derived_fact_relations"]["is_market_close"] is False
    assert payload["source_context"] == "synthetic_eval_fixture"
    assert payload["allowed_source_labels"] == relations["allowed_source_labels"]
    assert FACT_RELATION_DISCIPLINE in risk["messages"][0]["content"]
    qwen = runner.prepare_request("fundamental_event_analyst", load_fixture("GC04"), False, "offline")
    assert FACT_RELATION_DISCIPLINE not in qwen["messages"][0]["content"]
    assert qwen["derived_fact_relations"] is None
    assert qwen["prompt_hash"] == "18b41634f6154467c78ec5e06823be0c876b00b364781314c789e8bae087af0f"
    assert qwen["response_format"]["type"] == "json_schema"
    assert qwen["reasoning_effort"] == "low"


def test_recheck_plan_is_two_off_attempts_and_previous_qwen_can_be_reused(monkeypatch, tmp_path):
    runner = RoleIsolatedRunner(registry=_registry(monkeypatch),
                                tracker=UsageTracker(tmp_path / "usage.jsonl"))
    plan = runner.build_baseline_recheck_plan("p193g-offline")
    assert plan["planned_calls"] == plan["max_provider_attempts"] == 2
    assert plan["few_shot_on_calls"] == 0
    assert plan["provider_counts"] == {"deepseek": 1, "kimi": 1, "qwen": 0, "doubao": 0}
    assert [(task["case_id"], task["role"], task["reasoning_effort"])
            for task in plan["tasks"]] == [
                ("GC01", "technical_analyst", "none"),
                ("GC05", "risk_officer", "low"),
            ]
    fixture = load_fixture("GC04")
    qwen = runner.prepare_request("fundamental_event_analyst", fixture, False, "prior")
    prior = {"evaluation_run_id": "prior", "case_id": fixture["case_id"],
             "fixture_hash": fixture["fixture_hash"], "fact_bundle_hash": qwen["fact_bundle_hash"],
             "role": "fundamental_event_analyst", "provider": "qwen", "model": "qwen3.8-max",
             "prompt_hash": qwen["prompt_hash"], "schema_hash": qwen["schema_hash"],
             "few_shot_enabled": False, "selected_case_ids": [], "reasoning_effort": "low",
             "response_format_type": "json_schema", "max_output_tokens": 3072,
             "provider_attempts": 1, "fallback_used": False, "repair_used": False,
             "status": "PASS", "schema_valid": True, "finish_reason": "stop",
             "runtime_diagnostics": {"response_received": True, "timeout_config": {"read": 90.0}},
             "metrics": {"confidence_cap": True, "hallucination_rule_hits": []}}
    assert runner.qwen_baseline_reuse_check(prior)["reusable"] is True
    assert runner.qwen_baseline_reuse_check({**prior, "prompt_hash": "changed"})["reusable"] is False


def test_cli_default_prints_only_two_call_recheck_plan(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["p193_role_ab.py"])
    main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["planned_calls"] == plan["max_provider_attempts"] == 2
    assert plan["few_shot_on_calls"] == 0
    assert [task["provider"] for task in plan["tasks"]] == ["deepseek", "kimi"]


def test_risk_numeric_contradiction_is_semantic_hard_fail(monkeypatch, tmp_path):
    registry = _registry(monkeypatch)
    report = RiskReport.model_validate({
        "technical_risks": ["现价52.0已突破10日高点52.3"],
        "data_quality_risks": [], "positioning_risks": [], "bull_case_challenges": [],
        "invalid_assumptions": [], "missing_information": [],
        "risk_level": "high", "confidence": 50, "summary": "仍有高波动风险。",
    }).model_dump_json()
    clients = []

    def factory(provider):
        sdk = OpenAI(api_key="offline-test-key", base_url=provider.base_url,
                     timeout=provider.timeout, max_retries=0,
                     http_client=httpx2.Client(transport=httpx2.MockTransport(
                         lambda _request: httpx2.Response(200, json={
                             "choices": [{"message": {"role": "assistant", "content": report},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                         }))))
        clients.append(sdk)
        return OpenAICompatibleAdapter(provider, client=sdk)

    runner = RoleIsolatedRunner(registry=registry, client_factory=factory,
                                tracker=UsageTracker(tmp_path / "usage.jsonl"))
    try:
        result = runner.run_role("risk_officer", load_fixture("GC05"), few_shot_enabled=False,
                                 evaluation_run_id="offline", anonymous_arm="OFF",
                                 dry_run=False, allow_network=True)
        assert result["schema_valid"] is True
        assert result["metrics"]["numeric_relation_violation"] is True
        assert result["status"] == "SEMANTIC_FAIL"
        assert result["provider_attempts"] == 1
    finally:
        for sdk in clients:
            sdk.close()
