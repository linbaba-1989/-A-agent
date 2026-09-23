import json
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.a_share.p193_role_ab import RoleIsolatedRunner, load_fixture, load_synthetic_specialist_bundle
from evals.a_share.runtime_guardrails import OUTPUT_CAPS, usage_accounting, timeout_phase
from src.few_shot import FewShotRetriever, load_library, score_case
from src.few_shot_features import scenario_features
from src.technical_case_gate import technical_conflict
from src.provider_adapters import OpenAICompatibleAdapter
from src.provider_output_limits import (VERIFIED_OUTPUT_LIMITS, OutputLimitContract,
                                        UnsupportedOutputLimit, output_limit_status)
from src.model_registry import ProviderConfig
from src.usage_tracker import UsageTracker


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("real_network_forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.mark.parametrize("name,parameter", [("deepseek", "max_tokens"), ("qwen", "max_completion_tokens"),
                                           ("doubao", "max_tokens"), ("kimi", "max_completion_tokens")])
def test_sdk_serializes_explicit_mock_provider_contract(monkeypatch, name, parameter):
    # These are test contracts, NOT evidence of real provider parameter support.
    import httpx2
    from openai import OpenAI
    provider = ProviderConfig(name, "mock-model", "https://offline.invalid/v1", "TEST_KEY")
    monkeypatch.setitem(VERIFIED_OUTPUT_LIMITS, (name, provider.model_name, provider.base_url),
                        OutputLimitContract(parameter, "test-only"))
    from urllib.parse import urlsplit
    monkeypatch.setitem(VERIFIED_OUTPUT_LIMITS, (name, provider.model_name, provider.base_url),
        OutputLimitContract(parameter, "test-only", provider=name,
            official_host=urlsplit(provider.base_url).hostname, model_pattern=provider.model_name,
            semantics="test-only output"))
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx2.Response(200, json={"choices": [], "usage": {}})
    sdk = OpenAI(api_key="test-not-secret", base_url=provider.base_url, max_retries=0,
                 http_client=httpx2.Client(transport=httpx2.MockTransport(respond)))
    adapter = OpenAICompatibleAdapter(provider, client=sdk)
    adapter.chat_completion(model=provider.model_name, messages=[], max_output_tokens=4096)
    assert captured[-1][parameter] == 4096
    assert "max_output_tokens" not in captured[-1]
    adapter.chat_completion(model=provider.model_name, messages=[])
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & captured[-1].keys()
    with pytest.raises(ValueError):
        adapter.chat_completion(model=provider.model_name, messages=[], max_output_tokens=1, max_tokens=2)
    sdk.close()


def test_unknown_contract_blocks_before_client_construction(tmp_path):
    runner = RoleIsolatedRunner(tracker=UsageTracker(tmp_path / "log.jsonl"),
                                client_factory=lambda _: pytest.fail("must not construct client"))
    assert all(row["output_limit"]["status"] == "VERIFIED" for row in runner.provider_readiness().values())
    provider = replace(runner._candidate("technical_analyst")[1], model_name="unregistered-model")
    with pytest.raises(UnsupportedOutputLimit):
        runner.router._request(provider, [], max_output_tokens=4096)
    assert not (tmp_path / "log.jsonl").exists()


def test_usage_unknown_split_and_zero_are_preserved():
    empty = usage_accounting(None)
    assert all(empty[k] is None for k in ("input_tokens", "output_tokens", "total_tokens",
                                          "reasoning_tokens", "visible_output_tokens"))
    assert empty["usage_accounting_status"] == "unknown"
    result = usage_accounting({"usage": {"prompt_tokens": 0, "completion_tokens": 50,
        "completion_tokens_details": {"reasoning_tokens": 20, "text_tokens": 30}}})
    assert result["input_tokens"] == 0 and result["total_tokens"] is None
    assert result["reasoning_tokens"] == 20 and result["visible_output_tokens"] == 30
    assert result["usage_accounting_status"] == "provider_reported_split"
    assert usage_accounting({"usage": {"completion_tokens": 50}})["visible_output_tokens"] is None


@pytest.mark.parametrize("phase", ["connect", "read", "write", "pool"])
def test_timeout_phase_uses_cause_type_only(phase):
    import httpx2
    cause = getattr(httpx2, phase.title() + "Timeout")("Authorization SECRET")
    wrapper = RuntimeError("API Key SECRET")
    wrapper.__cause__ = cause
    assert timeout_phase(wrapper) == phase
    assert timeout_phase(RuntimeError("read timeout SECRET")) == "unknown"


def test_harness_cap_diagnostics_and_schema_failure_usage(monkeypatch, tmp_path):
    secret = "sentinel-secret-do-not-log"
    class Client:
        calls = []
        def chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    client = Client()
    runner = RoleIsolatedRunner(tracker=UsageTracker(tmp_path / "usage.jsonl"), client_factory=lambda _: client)
    _, provider = runner._candidate("technical_analyst")
    monkeypatch.setenv(provider.api_key_env, secret)
    def run():
        return runner.run_role("technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
            evaluation_run_id="test", anonymous_arm="X", dry_run=False, allow_network=True)
    result = run()
    assert result["status"] == "SCHEMA_FAIL" and result["output_tokens"] == 2
    assert result["runtime_diagnostics"]["response_received"] is True
    assert result["runtime_diagnostics"]["timeout_config"]["read"] == 90
    assert client.calls[0]["max_tokens"] == 4096
    assert result["provider_attempts"] == 1
    import httpx2
    def fail(**kwargs):
        exc = RuntimeError("Authorization " + secret)
        exc.__cause__ = httpx2.ReadTimeout(secret)
        raise exc
    client.chat_completion = fail
    result = run()
    diag = result["runtime_diagnostics"]
    assert diag["timeout_phase"] == "read" and diag["response_received"] is False
    assert diag["request_started_at"] and diag["request_finished_at"] and diag["elapsed_seconds"] >= 0
    assert result["output_tokens"] is None
    assert secret not in json.dumps(result) + (tmp_path / "usage.jsonl").read_text()


def test_gc01_gate_is_explainable_and_not_fixture_id_based():
    facts = load_fixture("GC01")["facts"]
    original = json.dumps(facts, sort_keys=True)
    text, audit = FewShotRetriever(enabled=True).retrieve("technical_analyst", facts)
    assert audit["selected_case_ids"] == ["technical-05"]
    gates = {row["case_id"]: row for row in audit["technical_conflict_audit"]}
    assert gates["technical-03"]["action"] == "hard_exclusion"
    assert gates["technical-09"]["action"] == "contradiction_penalty"
    assert gates["technical-09"]["score_after_gate"] < 6
    assert gates["technical-05"]["action"] == "compatible"
    assert gates["technical-05"]["current_dimensions"]["mid_term_state"] == "uptrend"
    assert json.dumps(facts, sort_keys=True) == original
    renamed = {**facts, "code": "UNRELATED", "name": "different"}
    assert FewShotRetriever(enabled=True).retrieve("technical_analyst", renamed)[1]["selected_case_ids"] == ["technical-05"]
    assert all(i.startswith("technical-") for i in audit["selected_case_ids"])


def test_explicit_mid_or_trend_conflict_cannot_be_outscored():
    features = scenario_features("technical_analyst", load_fixture("GC01")["facts"])
    case = next(c for c in load_library().cases["technical_analyst"] if c.case_id == "technical-05")
    for axis in ("mid_term_state", "trend_state"):
        for opposite in ("neutral", "range", "downtrend"):
            candidate = replace(case, technical_context={axis: opposite})
            assert technical_conflict(candidate, features)["action"] == "hard_exclusion"
            assert score_case(candidate, features) == 0


@pytest.mark.parametrize("gc,expected", [("GC02", "technical-10"), ("GC05", "technical-08")])
def test_technical_normal_scenarios_still_match(gc, expected):
    audit = FewShotRetriever(enabled=True).retrieve("technical_analyst", load_fixture(gc)["facts"])[1]
    assert expected in audit["selected_case_ids"]


def test_missing_data_allows_zero_cases():
    text, audit = FewShotRetriever(enabled=True).retrieve("technical_analyst", {})
    assert text == "" and audit["selected_case_count"] == 0 and audit["few_shot_status"] == "no_match"


def test_sentiment_label_does_not_exclude_and_chief_unchanged():
    runner = RoleIsolatedRunner()
    sentiment = runner.prepare_request("sentiment_analyst", load_fixture("GC03"), True, "test")
    assert sentiment["audit"]["possible_redundant_case"] is True
    assert sentiment["selected_case_ids"] == ["sentiment-09"]
    chief = runner.prepare_request("chief_researcher", load_fixture("GC05"), True, "test")
    assert chief["selected_case_ids"] == ["chief-03", "chief-01", "chief-06"]
    assert load_synthetic_specialist_bundle()["bundle_hash"] == "3ffce2a7a54e76d1a26f6c6b20c834809d7400f81c0bd9075e8c934207fff433"


def test_six_call_plan_caps_and_hash_identity():
    runner = RoleIsolatedRunner()
    summary = runner.run_round1(dry_run=True)["summary"]
    assert summary["planned_base_calls"] == summary["max_provider_attempts"] == 6
    assert summary["provider_counts"] == {"deepseek": 2, "qwen": 2, "kimi": 2}
    assert summary["actual_provider_calls"] == summary["retry_calls"] == 0
    for gc, role in (("GC01", "technical_analyst"), ("GC04", "fundamental_event_analyst"), ("GC05", "risk_officer")):
        off = runner.prepare_request(role, load_fixture(gc), False, "fixed")
        on = runner.prepare_request(role, load_fixture(gc), True, "fixed")
        assert off["fact_bundle_hash"] == on["fact_bundle_hash"]
        assert off["max_output_tokens"] == on["max_output_tokens"] == OUTPUT_CAPS[role]


def test_production_router_does_not_add_eval_cap():
    calls = []
    client = SimpleNamespace(chat_completion=lambda **kwargs: calls.append(kwargs))
    runner = RoleIsolatedRunner(client_factory=lambda _: client)
    provider = runner._candidate("technical_analyst")[1]
    runner.router._request(provider, [])
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & calls[0].keys()


def test_unverified_or_non_sdk_parameter_cannot_enable_cap(monkeypatch):
    provider = ProviderConfig("test", "model", None, "KEY")
    key = ("test", "model", None)
    for contract in (OutputLimitContract("max_tokens", ""), OutputLimitContract("max_output_tokens", "test")):
        monkeypatch.setitem(VERIFIED_OUTPUT_LIMITS, key, contract)
        assert output_limit_status(provider)["status"] == "UNSUPPORTED"
    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            OpenAICompatibleAdapter.output_budget(provider, limit)
