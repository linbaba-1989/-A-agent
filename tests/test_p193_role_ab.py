import json
import os
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evals.a_share import p193_role_ab as harness
from evals.a_share.p193_role_ab import (
    FIXTURES,
    RoleIsolatedRunner,
    build_blind_plan,
    evaluate_response,
    load_fixture,
    load_synthetic_specialist_bundle,
    public_evidence_record,
)
from src.agent import ROLE_SCHEMAS, StockResearchAgent
from src.agent_schemas import ChiefReport
from src.few_shot import FewShotRetriever
from src.llm_router import RouterResult
from src.llm_router import load_role_config
from src.model_registry import ModelRegistry, ProviderConfig
from src.usage_tracker import UsageTracker


def _technical_report():
    return {
        "trend": "up",
        "momentum": "neutral",
        "volume_price": "unavailable",
        "moving_average_structure": "mixed",
        "support": [],
        "resistance": [],
        "breakout_status": "unconfirmed",
        "bullish_signals": [],
        "bearish_signals": [],
        "data_gaps": ["外部数据"],
        "confidence": 40,
        "summary": "仅作测试。",
        "short_term_view": "neutral",
        "mid_term_view": "neutral",
        "trend_state": "uptrend",
        "invalidation_conditions": [{
            "condition": "收盘跌破MA20",
            "timeframe": "日线",
            "evidence_source": "价格",
        }],
        "missing_evidence": ["外部数据"],
    }


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=30, total_tokens=130),
        )


def _mock_registry(monkeypatch, configured=True):
    providers = {}
    for name, model in (("deepseek", "deepseek-v4-pro"), ("qwen", "qwen3.8-max"),
                        ("doubao", "doubao-test"), ("kimi", "kimi-k3")):
        env_name = f"P193_{name.upper()}_KEY"
        if configured:
            monkeypatch.setenv(env_name, "test-key")
        providers[name] = ProviderConfig(name, model, None, env_name)
    return ModelRegistry(providers)


def test_dry_run_is_exactly_ten_calls_and_provider_counts():
    runner = RoleIsolatedRunner()
    result = runner.run_round1(dry_run=True)
    summary = result["summary"]
    assert summary["planned_base_calls"] == 10
    assert summary["max_provider_attempts"] == 10
    assert summary["actual_provider_calls"] == 0
    assert summary["provider_counts"] == {"deepseek": 4, "qwen": 2, "doubao": 2, "kimi": 2}
    assert summary["fallback_calls"] == summary["repair_calls"] == summary["judge_model_calls"] == 0
    assert "few_shot_enabled" not in json.dumps(result["plan"], ensure_ascii=False)
    for row in summary["results"]:
        prefix = {
            "technical_analyst": "technical-", "fundamental_event_analyst": "fundamental-",
            "sentiment_analyst": "sentiment-", "risk_officer": "risk-",
            "chief_researcher": "chief-",
        }[row["role"]]
        assert all(case_id.startswith(prefix) for case_id in row["selected_case_ids"])


def test_budget_is_recomputed_from_final_role_prompts():
    budget = RoleIsolatedRunner().estimate_round1_budget()
    assert budget["planned_base_calls"] == 10
    assert budget["max_provider_attempts"] == 10
    assert budget["off_input_tokens"] > 0
    assert budget["on_input_tokens"] > budget["off_input_tokens"]
    assert budget["few_shot_extra_tokens"] == budget["on_input_tokens"] - budget["off_input_tokens"]
    assert budget["output_tokens"] == 2 * (200 + 120 + 130 + 170 + 550)
    assert budget["cost_status"] in ("unknown", "estimated")
    assert budget["chief_bundle_hash"] == load_synthetic_specialist_bundle()["bundle_hash"]


def test_role_runner_prepares_only_one_role_and_preserves_off_on_fact_hashes():
    runner = RoleIsolatedRunner()
    fixture = load_fixture("GC01")
    off = runner.prepare_request("technical_analyst", fixture, False, "test-run")
    on = runner.prepare_request("technical_analyst", fixture, True, "test-run")
    assert "RELEVANT_CASES" not in off["messages"][0]["content"]
    assert "RELEVANT_CASES" in on["messages"][0]["content"]
    assert off["fact_bundle_hash"] == on["fact_bundle_hash"] == fixture["fact_bundle_hash"]
    assert all(case.startswith("technical-") for case in on["selected_case_ids"])


def test_chief_uses_same_synthetic_bundle_for_both_arms():
    runner = RoleIsolatedRunner()
    fixture = load_fixture("GC05")
    bundle = load_synthetic_specialist_bundle()
    off = runner.prepare_request("chief_researcher", fixture, False, "test-run", bundle)
    on = runner.prepare_request("chief_researcher", fixture, True, "test-run", bundle)
    assert off["chief_bundle_hash"] == on["chief_bundle_hash"] == bundle["bundle_hash"]
    assert off["fact_bundle_hash"] == on["fact_bundle_hash"] == fixture["fact_bundle_hash"]
    assert off["messages"][1]["content"] == on["messages"][1]["content"]


def test_live_role_runner_makes_one_provider_call_without_other_roles(monkeypatch, tmp_path):
    registry = _mock_registry(monkeypatch)
    client = _FakeClient(_technical_report())
    runner = RoleIsolatedRunner(registry=registry, tracker=UsageTracker(tmp_path / "usage.jsonl"),
                                client_factory=lambda _provider: client)
    result = runner.run_role("technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
                             evaluation_run_id="test-live", anonymous_arm="X",
                             dry_run=False, allow_network=True)
    assert result["status"] == "PASS"
    assert result["fallback_used"] is False and result["repair_used"] is False
    assert len(client.calls) == 1


def test_provider_failure_does_not_fallback(monkeypatch, tmp_path):
    registry = _mock_registry(monkeypatch, configured=False)
    client = _FakeClient(_technical_report())
    runner = RoleIsolatedRunner(registry=registry, tracker=UsageTracker(tmp_path / "usage.jsonl"),
                                client_factory=lambda _provider: client)
    result = runner.run_role("technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
                             evaluation_run_id="test-provider-error", anonymous_arm="X",
                             dry_run=False, allow_network=True)
    assert result["status"] == "PROVIDER_ERROR"
    assert result["fallback_used"] is False
    assert client.calls == []


def test_schema_failure_is_raw_and_has_no_ai_repair(monkeypatch, tmp_path):
    registry = _mock_registry(monkeypatch)
    client = _FakeClient({})
    runner = RoleIsolatedRunner(registry=registry, tracker=UsageTracker(tmp_path / "usage.jsonl"),
                                client_factory=lambda _provider: client)
    result = runner.run_role("technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
                             evaluation_run_id="test-schema-fail", anonymous_arm="X",
                             dry_run=False, allow_network=True)
    assert result["status"] == "SCHEMA_FAIL"
    assert result["schema_valid"] is False
    assert result["repair_used"] is False
    assert len(client.calls) == 1


def test_blind_mapping_is_reproducible_and_private():
    first = build_blind_plan("fixed-seed")
    second = build_blind_plan("fixed-seed")
    assert first.public_dict() == second.public_dict()
    assert first.private_mapping == second.private_mapping
    assert "few_shot_enabled" not in json.dumps(first.public_dict(), ensure_ascii=False)


def test_deterministic_evaluator_flags_hard_rules_without_judge():
    fixture = load_fixture("GC02")
    report = {
        "confidence": 99,
        "confidence_cap": 99,
        "asserted_external_facts": ["technical-10"],
        "summary": "confirmed breakout; 必涨",
        "breakout_status": "confirmed",
        "short_term_view": "unavailable",
        "mid_term_view": "unavailable",
        "trend_state": "uptrend",
    }
    metrics = evaluate_response("technical_analyst", report, fixture["facts"], ["technical-10"])
    assert metrics["schema_compliance"] is False
    assert metrics["hallucination_rule_hits"] == ["technical-10"]
    assert metrics["case_fact_leak"] is False
    assert metrics["confidence_cap"] is False
    assert metrics["trend_consistency"] is False
    assert metrics["forbidden_trade_instruction"]


def test_public_evidence_strips_private_arm_bit():
    record = public_evidence_record({"anonymous_arm": "X", "few_shot_enabled": True,
                                      "raw_response": "{}", "parsed_response": {}})
    assert "few_shot_enabled" not in record
    assert record["anonymous_arm"] == "X"


def test_production_analyze_still_runs_full_five_role_workflow():
    calls = []

    class CaptureRouter:
        def call(self, role, messages, schema, analysis_id, analysis_mode):
            calls.append(role)
            return RouterResult(role, "mock", "mock", True, {}, 0.0)

    result = StockResearchAgent(CaptureRouter(), few_shot_retriever=FewShotRetriever(enabled=False)).analyze(
        "SYNTHETIC", {"last_price": 10, "fundamental_data": "unavailable"})
    assert set(calls) == set(ROLE_SCHEMAS) | {"chief_researcher"}
    assert len(calls) == 5
    assert set(result["employees"]) == set(ROLE_SCHEMAS)


def test_original_fixture_hashes_are_still_present():
    assert {load_fixture(case)["fixture_hash"] for case in FIXTURES} == {
        "1316aba2f099e1c3e63dcaefb00a37a3fe0deb078913446e4d123b017f2dd303",
        "44bd26e72975f1803aaa8ff4073ad53d790484537ae4893489a582f182494a1e",
        "847c70205885077939ac8b2c4a7ad42b7830fa5426d494fa493009e1ab376746",
        "184448bd0893f9bf5eeccd168f3401f81e8b356d8fa900ec736584d85fb5b855",
        "d6b85c09dd815d8c4c8383a36ebf7b0580dc6f013d636a27250dbf148d737faf",
    }


def test_eval_bootstrap_loads_repo_dotenv_independent_of_cwd(monkeypatch, tmp_path):
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / ".env").write_text("P193_BOOTSTRAP_PROBE=loaded\n", encoding="utf-8")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.setattr(harness, "ROOT", fake_root)
    monkeypatch.chdir(other_cwd)
    with patch.dict(os.environ, {}, clear=True):
        harness.bootstrap_eval_environment()
        assert os.getenv("P193_BOOTSTRAP_PROBE") == "loaded"


def test_standalone_preflight_runs_from_another_cwd(tmp_path):
    process = subprocess.run(
        [sys.executable, str(harness.ROOT / "evals" / "a_share" / "p193_role_ab.py"),
         "--preflight"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert process.returncode == 0
    report = json.loads(process.stdout)
    assert set(report) == {"deepseek", "qwen", "doubao", "kimi"}
    assert all(set(row) == {"key", "model", "endpoint", "mapping", "adapter",
                            "prompt", "schema", "readiness"} for row in report.values())


def test_eval_and_production_resolve_the_same_primary_mapping():
    runner = RoleIsolatedRunner()
    production_config = load_role_config()
    production_registry = ModelRegistry()
    for role in ("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
                 "risk_officer", "chief_researcher"):
        candidate, provider = runner._candidate(role)
        primary = production_config[role]["candidates"][0]
        assert candidate["provider"] == primary["provider"] == provider.provider_name
        expected_model = (primary.get("model") or os.getenv(primary.get("model_env", "")) or
                          production_registry.get(provider.provider_name).model_name)
        assert provider.model_name == expected_model


def test_sanitized_preflight_builds_adapter_prompt_schema_without_network(monkeypatch, tmp_path, capsys):
    sentinel = "test-secret-never-print"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)
    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("preflight_must_not_connect")
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    usage_path = tmp_path / "usage.jsonl"
    runner = RoleIsolatedRunner(tracker=UsageTracker(usage_path))
    report = runner.provider_readiness()
    assert report["deepseek"]["key"] == "configured"
    assert all(row["adapter"] == row["prompt"] == row["schema"] == "ready"
               for row in report.values())
    assert sentinel not in json.dumps(report, ensure_ascii=False)
    assert sentinel not in capsys.readouterr().out
    assert not usage_path.exists()


def test_doubao_placeholder_blocks_config_without_exposing_model_value(monkeypatch):
    monkeypatch.setenv("DOUBAO_MODEL", "YOUR_DOUBAO_ENDPOINT_ID")
    runner = RoleIsolatedRunner()
    report = runner.provider_readiness()
    assert report["doubao"]["model"] == "placeholder"
    assert report["doubao"]["readiness"] == "BLOCKED_CONFIG"
    assert "YOUR_DOUBAO_ENDPOINT_ID" not in json.dumps(report, ensure_ascii=False)


def test_production_few_shot_default_remains_off(monkeypatch):
    monkeypatch.delenv("A_SHARE_FEW_SHOT_ENABLED", raising=False)
    assert FewShotRetriever().enabled is False
