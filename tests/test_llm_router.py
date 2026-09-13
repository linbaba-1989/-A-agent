import json
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from src.llm_router import LLMRouter
from src.model_registry import ModelRegistry, ProviderConfig
from src.usage_tracker import UsageTracker


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def response(content='{"value":"ok"}', prompt=10, completion=5):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                                 total_tokens=prompt + completion))


def make_router(monkeypatch, tmp_path, outcomes, retries=0):
    monkeypatch.setenv("PRIMARY_KEY", "configured")
    monkeypatch.setenv("FALLBACK_KEY", "configured")
    providers = {
        "primary": ProviderConfig("primary", "p-model", None, "PRIMARY_KEY", max_retries=retries,
                                  cost_profile={"input_per_million": 1, "output_per_million": 2}),
        "fallback": ProviderConfig("fallback", "f-model", None, "FALLBACK_KEY", max_retries=0),
    }
    calls = []

    def factory(provider):
        def create(**kwargs):
            calls.append((provider.provider_name, kwargs))
            outcome = outcomes[provider.provider_name].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    router = LLMRouter(ModelRegistry(providers), {"role": {"primary": "primary", "fallback": "fallback"}},
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    return router, calls


def test_primary_success_role_route_and_token_record(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path, {"primary": [response()], "fallback": []})
    result = router.call("role", [{"role": "user", "content": "x"}], Output, "A1")
    assert result.success and result.provider == "primary" and not result.fallback
    assert result.total_tokens == 15 and result.estimated_cost == 0.00002
    assert calls[0][0] == "primary"
    assert router.tracker.records()[0]["role"] == "role"


def test_unconfigured_pricing_returns_unknown_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIMARY_KEY", "configured")
    provider = ProviderConfig("primary", "p-model", None, "PRIMARY_KEY", cost_profile=None)
    def factory(_provider):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response())))
    router = LLMRouter(ModelRegistry({"primary": provider}),
                       {"role": {"candidates": [{"provider": "primary"}]}},
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    result = router.call("role", [], Output, "unknown-cost")
    assert result.estimated_cost is None and result.cost_status == "unknown"
    assert router.tracker.records()[0]["estimated_cost"] is None


def test_primary_failure_uses_fallback(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [TimeoutError("timeout")], "fallback": [response()]})
    result = router.call("role", [], Output, "A2")
    assert result.success and result.provider == "fallback" and result.fallback
    assert [name for name, _ in calls] == ["primary", "fallback"]


def test_primary_is_attempted_once_before_fallback(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [TimeoutError("first")], "fallback": [response()]}, retries=1)
    result = router.call("role", [], Output, "A2-retry")
    assert result.success and result.provider == "fallback"
    assert [name for name, _ in calls] == ["primary", "fallback"]


def test_provider_temperature_capability_is_used_by_healthcheck(monkeypatch, tmp_path):
    monkeypatch.setenv("KIMI_KEY", "configured")
    monkeypatch.setenv("DEEPSEEK_KEY", "configured")
    monkeypatch.setenv("QWEN_KEY", "configured")
    providers = {
        "kimi": ProviderConfig("kimi", "kimi-k2.6", None, "KIMI_KEY", fixed_temperature=1.0),
        "deepseek": ProviderConfig("deepseek", "deepseek-chat", None, "DEEPSEEK_KEY"),
        "qwen": ProviderConfig("qwen", "qwen-plus", None, "QWEN_KEY"),
    }
    calls = []

    def factory(provider):
        def create(**kwargs):
            calls.append((provider.provider_name, kwargs))
            return response('{"ok":true}')
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    router = LLMRouter(ModelRegistry(providers),
                       {"role": {"primary": "kimi", "fallback": "deepseek"}},
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    assert router.healthcheck("kimi")["success"]
    assert router.healthcheck("deepseek")["success"]
    assert router.healthcheck("qwen")["success"]
    temperatures = {name: kwargs["temperature"] for name, kwargs in calls}
    assert temperatures == {"kimi": 1.0, "deepseek": 0, "qwen": 0}


def test_deepseek_v4_profiles_send_reasoning_without_temperature(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_KEY", "configured")
    provider = ProviderConfig("deepseek", "deepseek-v4-pro", None, "DEEPSEEK_KEY", max_retries=0,
                              supports_custom_temperature=False, supports_reasoning_effort=True,
                              reasoning_levels=("low", "high", "max"), reasoning_mode="deepseek")
    calls = []

    def factory(_provider):
        def create(**kwargs):
            calls.append(kwargs)
            return response()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    routes = {"role": {"candidates": [{"provider": "deepseek", "model": "deepseek-v4-pro",
                                          "reasoning": {"standard": "high", "max": "max"}}]}}
    router = LLMRouter(ModelRegistry({"deepseek": provider}), routes,
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    standard = router.call("role", [], Output, "S", "standard")
    maximum = router.call("role", [], Output, "M", "max")
    assert standard.reasoning_effort == "high" and maximum.reasoning_effort == "max"
    assert [call["reasoning_effort"] for call in calls] == ["high", "max"]
    assert all("temperature" not in call for call in calls)
    assert all(call["extra_body"] == {"thinking": {"type": "enabled"}} for call in calls)


def test_qwen_model_candidate_fallback_records_actual_model(monkeypatch, tmp_path):
    monkeypatch.setenv("QWEN_KEY", "configured")
    provider = ProviderConfig("qwen", "qwen3.8-max", None, "QWEN_KEY", max_retries=0)
    calls = []

    def factory(actual):
        def create(**kwargs):
            calls.append(kwargs["model"])
            if actual.model_name == "qwen3.8-max":
                raise PermissionError("model unavailable")
            return response()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    routes = {"role": {"candidates": [{"provider": "qwen", "model": "qwen3.8-max"},
                                         {"provider": "qwen", "model": "qwen3.7-plus"}]}}
    router = LLMRouter(ModelRegistry({"qwen": provider}), routes,
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    result = router.call("role", [], Output, "Q")
    assert result.success and result.fallback
    assert result.requested_model == "qwen3.8-max"
    assert result.actual_model == "qwen3.7-plus"
    assert result.fallback_reason and "PermissionError" in result.fallback_reason
    assert calls == ["qwen3.8-max", "qwen3.7-plus"]


def test_kimi_k3_omits_unverified_temperature_and_reasoning(monkeypatch, tmp_path):
    monkeypatch.setenv("KIMI_KEY", "configured")
    base = ProviderConfig("kimi", "kimi-k3", None, "KIMI_KEY", max_retries=0)
    calls = []

    def factory(_provider):
        def create(**kwargs):
            calls.append(kwargs)
            return response('{"ok":true}')
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    router = LLMRouter(ModelRegistry({"kimi": base}),
                       {"role": {"candidates": [{"provider": "kimi", "model": "kimi-k3"}]}},
                       UsageTracker(tmp_path / "usage.jsonl"), factory)
    assert router.healthcheck("kimi", "kimi-k3")["success"]
    assert "temperature" not in calls[0]
    assert "reasoning_effort" not in calls[0]


def test_bad_json_gets_one_format_repair(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [response("not-json"), response('{"value":"fixed"}')], "fallback": []})
    result = router.call("role", [], Output, "A3")
    assert result.data == {"value": "fixed"}
    assert len(calls) == 2
    assert result.schema_repair_count == 1


def test_wrong_type_missing_and_extra_fields_are_repaired(monkeypatch, tmp_path):
    for index, invalid in enumerate(('{"value":42}', '{}', '{"value":"x","extra":1}')):
        router, calls = make_router(monkeypatch, tmp_path,
                                    {"primary": [response(invalid), response('{"value":"fixed"}')],
                                     "fallback": []})
        result = router.call("role", [], Output, f"repair-{index}")
        assert result.success and result.data == {"value": "fixed"}
        repair_prompt = calls[1][1]["messages"][-1]["content"]
        assert "Validation errors" in repair_prompt and "Correct JSON Schema" in repair_prompt


def test_schema_repair_failure_records_usage(monkeypatch, tmp_path):
    router, _ = make_router(monkeypatch, tmp_path,
                            {"primary": [response('{}', 10, 2), response('{}', 12, 3)],
                             "fallback": []})
    result = router.call("role", [], Output, "repair-fail")
    assert not result.success and "schema_validation_failed" in result.error
    attempt = router.tracker.records()[0]
    assert attempt["usage_status"] == "available"
    assert attempt["total_tokens"] == 27
    assert attempt["schema_repair_count"] == 1


def test_timeout_usage_is_unavailable(monkeypatch, tmp_path):
    router, _ = make_router(monkeypatch, tmp_path,
                            {"primary": [TimeoutError("slow")], "fallback": [TimeoutError("slow too")]})
    result = router.call("role", [], Output, "timeouts")
    assert not result.success
    attempts = [row for row in router.tracker.records() if row["provider"] != "unavailable"]
    assert all(row["usage_status"] == "unavailable" and row["total_tokens"] is None for row in attempts)
    assert all(row["timeout_stage"] == "response" for row in attempts)


def test_schema_is_added_to_every_request(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path, {"primary": [response()], "fallback": []})
    router.call("role", [], Output, "schema")
    schema_prompt = calls[0][1]["messages"][-1]["content"]
    assert "JSON Schema" in schema_prompt and "additionalProperties" in schema_prompt


def test_no_key_and_all_models_failed(monkeypatch, tmp_path):
    router, _ = make_router(monkeypatch, tmp_path,
                            {"primary": [TimeoutError("p")], "fallback": [TimeoutError("f")]})
    monkeypatch.delenv("PRIMARY_KEY")
    result = router.call("role", [], Output, "A4")
    assert not result.success and result.data is None
    assert "not_configured" in result.error and "TimeoutError" in result.error
