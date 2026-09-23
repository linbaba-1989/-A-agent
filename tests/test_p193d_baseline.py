"""P1.9.3D evaluation contracts, checked without provider network access."""

import json
import socket
from dataclasses import asdict

import httpx2
import pytest
from openai import OpenAI

from evals.a_share.p193_role_ab import (
    FIXTURES,
    ROLE_SCHEMAS_WITH_CHIEF,
    STRUCTURED_OUTPUT_DISCIPLINE,
    RoleIsolatedRunner,
    baseline_reliability_gate,
    build_blind_plan,
    canonical_hash,
    deterministic_enabled_order,
    load_fixture,
)
from src.model_registry import ModelRegistry, ProviderConfig
from src.provider_adapters import OpenAICompatibleAdapter
from src.usage_tracker import UsageTracker


OFFLINE_ROLES = (
    ("technical_analyst", "GC01", "deepseek", "deepseek-v4-pro", "https://api.deepseek.com", "max_tokens", 4096, 90),
    ("fundamental_event_analyst", "GC04", "qwen", "qwen3.8-max", "https://dashscope.aliyuncs.com/compatible-mode/v1", "max_completion_tokens", 3072, 90),
    ("risk_officer", "GC05", "kimi", "kimi-k3", "https://api.moonshot.cn/v1", "max_completion_tokens", 4096, 150),
)
FIXTURE_HASHES = {
    "GC01": "1316aba2f099e1c3e63dcaefb00a37a3fe0deb078913446e4d123b017f2dd303",
    "GC02": "44bd26e72975f1803aaa8ff4073ad53d790484537ae4893489a582f182494a1e",
    "GC03": "847c70205885077939ac8b2c4a7ad42b7830fa5426d494fa493009e1ab376746",
    "GC04": "184448bd0893f9bf5eeccd168f3401f81e8b356d8fa900ec736584d85fb5b855",
    "GC05": "d6b85c09dd815d8c4c8383a36ebf7b0580dc6f013d636a27250dbf148d737faf",
}
SCHEMA_HASHES = {
    "technical_analyst": "0805eefa5268164bd5d511dc58b542678ad96a9a252ee7765a10bd8987733d4b",
    "fundamental_event_analyst": "e2fd872f7a5062c085870f94c99e150be493c7cb36564f913afdd480ae876dd1",
    "sentiment_analyst": "030f271bd6f74ea0e0377dc56bf46a548ce768c1b0f6cc29a4fb48f6921450ed",
    "risk_officer": "4f1b8b278666b164813455946be1e07861147d8304fd89c5b7913780c4864908",
    "chief_researcher": "a391f98a7a05c8d36a963f3504e415ea44105bebfd664627de0a3d20facdcd6a",
}


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("real_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _offline_registry(monkeypatch):
    providers = {}
    for _, _, name, model, url, *_ in OFFLINE_ROLES:
        env = f"P193D_{name.upper()}_KEY"
        monkeypatch.setenv(env, "offline-test-key")
        providers[name] = ProviderConfig(name, model, url, env)
    providers["doubao"] = ProviderConfig(
        "doubao", "offline-doubao", "https://ark.cn-beijing.volces.com/api/v3", "P193D_DOUBAO_KEY"
    )
    return ModelRegistry(providers)


@pytest.mark.parametrize("role,case_id,name,model,url,cap_field,cap,eval_timeout", OFFLINE_ROLES)
def test_eval_native_request_serializes_through_sdk_mock_transport(
    role, case_id, name, model, url, cap_field, cap, eval_timeout, monkeypatch, tmp_path
):
    registry = _offline_registry(monkeypatch)
    production_before = asdict(registry.for_model(name, model))
    bodies = []
    client_timeouts = []
    clients = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

    def client_factory(provider):
        client_timeouts.append(provider.timeout)
        sdk = OpenAI(
            api_key="offline-test-key", base_url=provider.base_url,
            timeout=provider.timeout, max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )
        clients.append(sdk)
        return OpenAICompatibleAdapter(provider, client=sdk)

    runner = RoleIsolatedRunner(
        registry=registry, client_factory=client_factory,
        tracker=UsageTracker(tmp_path / "usage.jsonl"),
    )
    fixture = load_fixture(case_id)
    prepared = runner.prepare_request(role, fixture, False, "offline-serialization")
    assert prepared["provider"].base_url == url
    assert prepared["provider"].timeout == eval_timeout
    assert prepared["provider"] is not registry.get(name)
    assert prepared["reasoning_effort"] == "low"
    assert prepared["max_output_tokens"] == cap
    assert asdict(registry.for_model(name, model)) == production_before

    try:
        result = runner.run_role(
            role, fixture, few_shot_enabled=False,
            evaluation_run_id="offline-serialization", anonymous_arm="X",
            dry_run=False, allow_network=True,
        )
        assert result["provider_attempts"] == 1
        assert result["fallback_used"] is result["repair_used"] is False
        assert result["runtime_diagnostics"]["response_received"] is True
        assert result["runtime_diagnostics"]["timeout_config"]["read"] == eval_timeout
        assert client_timeouts == [eval_timeout]
        assert len(bodies) == 1
        body = bodies[0]
        assert body["model"] == model
        assert body["response_format"] == prepared["response_format"]
        assert body["reasoning_effort"] == "low"
        assert body[cap_field] == cap
        assert {"max_tokens", "max_completion_tokens", "max_output_tokens"} & body.keys() == {cap_field}
        assert all(message["role"] in ("system", "user") for message in body["messages"])
        if name == "qwen":
            response_format = body["response_format"]
            assert response_format["type"] == "json_schema"
            assert response_format["json_schema"]["strict"] is True
            assert response_format["json_schema"]["name"] == prepared["schema"].__name__
            wire_schema = response_format["json_schema"]["schema"]
            source_schema = prepared["schema"].model_json_schema()
            assert wire_schema == source_schema
            assert set(wire_schema["properties"]) == set(prepared["schema"].model_fields)
            assert set(wire_schema["required"]) == {
                field for field, definition in prepared["schema"].model_fields.items()
                if definition.is_required()
            }
            assert wire_schema["additionalProperties"] is False
            assert all(definition["additionalProperties"] is False
                       for definition in wire_schema.get("$defs", {}).values()
                       if "properties" in definition)
            assert all(definition.get("additionalProperties") is False
                       for definition in wire_schema.get("$defs", {}).values()
                       if definition.get("type") == "object")
        else:
            assert body["response_format"] == {"type": "json_object"}
            if name == "deepseek":
                assert body["thinking"] == {"type": "enabled"}

        # The same router, called without evaluation overrides, retains the
        # production cap, reasoning, schema mode, and timeout behavior.
        runner.router._request(runner._candidate(role)[1], [])
        assert len(bodies) == 2
        production_body = bodies[1]
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & production_body.keys()
        assert "reasoning_effort" not in production_body
        assert production_body["response_format"] == {"type": "json_object"}
        assert client_timeouts == [eval_timeout, 90.0]
        assert asdict(registry.for_model(name, model)) == production_before
    finally:
        for client in clients:
            client.close()


@pytest.mark.parametrize("role,case_id", [
    ("technical_analyst", "GC01"),
    ("fundamental_event_analyst", "GC04"),
    ("sentiment_analyst", "GC03"),
    ("risk_officer", "GC05"),
    ("chief_researcher", "GC05"),
])
def test_output_discipline_is_identical_in_off_and_on_prompts(role, case_id):
    runner = RoleIsolatedRunner()
    fixture = load_fixture(case_id)
    off = runner.prepare_request(role, fixture, False, "discipline-audit")
    on = runner.prepare_request(role, fixture, True, "discipline-audit")
    for prepared in (off, on):
        system_message = prepared["messages"][0]
        assert system_message["role"] == "system"
        assert system_message["content"].endswith("\n\n" + STRUCTURED_OUTPUT_DISCIPLINE)
        assert system_message["content"].count(STRUCTURED_OUTPUT_DISCIPLINE) == 1
    assert off["fact_bundle_hash"] == on["fact_bundle_hash"]
    assert off["schema"] is on["schema"]
    assert off["response_format"] == on["response_format"]
    assert off["max_output_tokens"] == on["max_output_tokens"]


def test_fixture_and_pydantic_schema_baselines_are_unchanged():
    assert set(FIXTURES) == set(FIXTURE_HASHES)
    assert {case_id: load_fixture(case_id)["fixture_hash"] for case_id in FIXTURES} == FIXTURE_HASHES
    assert set(ROLE_SCHEMAS_WITH_CHIEF) == set(SCHEMA_HASHES)
    assert {role: canonical_hash(schema.model_json_schema())
            for role, schema in ROLE_SCHEMAS_WITH_CHIEF.items()} == SCHEMA_HASHES


def test_blind_mapping_covers_five_cases_and_both_orders_reproducibly():
    seed = build_blind_plan("p193d-blind-audit").seed
    pairs = [(case_id, role) for case_id in FIXTURES
             for role in load_fixture(case_id)["target_roles"]]
    assert len(pairs) == 15
    first = {pair: deterministic_enabled_order(seed, *pair) for pair in pairs}
    again = {pair: deterministic_enabled_order(seed, *pair) for pair in reversed(pairs)}
    assert first == again
    assert all(order in ((False, True), (True, False)) for order in first.values())
    assert {order[0] for order in first.values()} == {False, True}
    assert {order[0] for (case_id, _role), order in first.items()
            if case_id == "GC05"} == {False, True}


def test_baseline_smoke_plan_has_three_off_only_single_attempts():
    runner = RoleIsolatedRunner()
    plan = runner.build_baseline_smoke_plan("p193e-off-only")
    assert plan["planned_calls"] == 3
    assert plan["max_provider_attempts"] == 3
    assert plan["few_shot_on_calls"] == 0
    assert plan["provider_counts"] == {"deepseek": 1, "qwen": 1, "kimi": 1}
    tasks = plan["tasks"]
    assert len(tasks) == 3
    assert [(task["case_id"], task["role"], task["provider"]) for task in tasks] == [
        ("GC01", "technical_analyst", "deepseek"),
        ("GC04", "fundamental_event_analyst", "qwen"),
        ("GC05", "risk_officer", "kimi"),
    ]
    assert all(task["few_shot_enabled"] is False for task in tasks)
    assert all(task["planned_provider_attempts"] == 1 for task in tasks)
    dry_run = runner.run_baseline_smoke("p193e-off-only", dry_run=True)
    assert dry_run["actual_provider_attempts"] == 0
    assert dry_run["gate"]["status"] == "NOT_RUN"
    assert all(result["few_shot_enabled"] is False for result in dry_run["results"])


def _passing_baseline_rows():
    rows = []
    for role, case_id, name, model, *_ in OFFLINE_ROLES:
        rows.append({
            "case_id": case_id, "role": role, "fixture_hash": load_fixture(case_id)["fixture_hash"],
            "fact_bundle_hash": load_fixture(case_id)["fact_bundle_hash"],
            "provider": name, "model": model, "few_shot_enabled": False,
            "reasoning_effort": "low",
            "response_format_type": "json_schema" if name == "qwen" else "json_object",
            "schema_hash": canonical_hash(ROLE_SCHEMAS_WITH_CHIEF[role].model_json_schema()),
            "max_output_tokens": 3072 if name == "qwen" else 4096,
            "provider_attempts": 1, "fallback_used": False, "repair_used": False,
            "status": "PASS", "schema_valid": True, "finish_reason": "stop",
            "runtime_diagnostics": {"response_received": True, "timeout_phase": "unknown",
                                    "timeout_config": {"read": 150 if name == "kimi" else 90}},
            "metrics": {"hallucination_rule_hits": [], "confidence_cap": True},
        })
    return rows


def test_baseline_reliability_gate_requires_complete_provider_and_fact_success():
    rows = _passing_baseline_rows()
    assert baseline_reliability_gate([])["status"] == "NOT_RUN"
    assert baseline_reliability_gate(rows)["status"] == "PENDING_FACT_REVIEW"
    reviewed = {row["role"]: True for row in rows}
    assert baseline_reliability_gate(rows, reviewed)["status"] == "PASS"
    assert baseline_reliability_gate(rows[:2], reviewed)["status"] == "FAIL"
    for key, bad_value in (
        ("few_shot_enabled", True), ("provider_attempts", 2),
        ("fallback_used", True), ("repair_used", True),
        ("status", "PROVIDER_ERROR"), ("schema_valid", False),
        ("finish_reason", "length"),
    ):
        changed = [dict(row) for row in rows]
        changed[0][key] = bad_value
        assert baseline_reliability_gate(changed, reviewed)["status"] == "FAIL", key
    for section, key, bad_value in (
        ("runtime_diagnostics", "response_received", False),
        ("metrics", "hallucination_rule_hits", ["fabricated-fact"]),
        ("metrics", "confidence_cap", False),
    ):
        changed = [dict(row) for row in rows]
        changed[0][section] = dict(changed[0][section], **{key: bad_value})
        assert baseline_reliability_gate(changed, reviewed)["status"] == "FAIL", key
    assert baseline_reliability_gate(rows, {**reviewed, rows[0]["role"]: False})["status"] == "FAIL"


def test_live_ab_is_blocked_until_all_off_baselines_pass_manual_review(tmp_path):
    # The denial happens before any client is constructed or SDK call is sent.
    runner = RoleIsolatedRunner(
        client_factory=lambda _provider: pytest.fail("provider_call_before_baseline_gate"),
        tracker=UsageTracker(tmp_path / "usage.jsonl"),
    )
    with pytest.raises(PermissionError, match="baseline_reliability_gate_not_passed"):
        runner.run_round1("blocked-no-results", dry_run=False, allow_network=True)
    rows = _passing_baseline_rows()
    with pytest.raises(PermissionError, match="baseline_reliability_gate_not_passed"):
        runner.run_round1("blocked-no-review", dry_run=False, allow_network=True,
                          baseline_results=rows)
    with pytest.raises(PermissionError, match="baseline_reliability_gate_not_passed"):
        runner.run_role(
            "technical_analyst", load_fixture("GC01"), few_shot_enabled=True,
            evaluation_run_id="blocked-on", anonymous_arm="X",
            dry_run=False, allow_network=True,
        )
    assert not (tmp_path / "usage.jsonl").exists()


def test_public_ab_dry_run_does_not_expose_private_off_on_bit():
    result = RoleIsolatedRunner().run_round1("p193d-private-arm", dry_run=True)
    assert "few_shot_enabled" not in json.dumps(result, ensure_ascii=False)


def test_length_finish_is_failure_even_with_valid_parseable_schema(monkeypatch, tmp_path):
    registry = _offline_registry(monkeypatch)
    report = ROLE_SCHEMAS_WITH_CHIEF["technical_analyst"].model_validate({
        "trend": "up", "momentum": "neutral", "volume_price": "unavailable",
        "moving_average_structure": "mixed", "support": [], "resistance": [],
        "breakout_status": "unconfirmed", "bullish_signals": [], "bearish_signals": [],
        "data_gaps": [], "confidence": 40, "summary": "测试",
    }).model_dump_json()
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": report},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 4096, "total_tokens": 4097},
        })

    clients = []

    def client_factory(provider):
        sdk = OpenAI(
            api_key="offline-test-key", base_url=provider.base_url,
            timeout=provider.timeout, max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),
        )
        clients.append(sdk)
        return OpenAICompatibleAdapter(provider, client=sdk)

    runner = RoleIsolatedRunner(
        registry=registry, client_factory=client_factory,
        tracker=UsageTracker(tmp_path / "usage.jsonl"),
    )
    try:
        result = runner.run_role(
            "technical_analyst", load_fixture("GC01"), few_shot_enabled=False,
            evaluation_run_id="offline-length", anonymous_arm="OFF",
            dry_run=False, allow_network=True,
        )
        assert len(requests) == 1
        assert result["provider_attempts"] == 1
        assert result["finish_reason"] == "length"
        assert result["status"] == "SCHEMA_FAIL"
        assert result["schema_valid"] is False
        assert result["error"] == "output_cap_hit"
        assert result["parsed_response"] is None
        assert result["repair_used"] is result["fallback_used"] is False
    finally:
        for client in clients:
            client.close()
