import json
from types import SimpleNamespace

from pydantic import BaseModel

from src.llm_router import LLMRouter
from src.model_registry import ModelRegistry, ProviderConfig
from src.usage_tracker import UsageTracker


class Output(BaseModel):
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


def test_primary_failure_uses_fallback(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [TimeoutError("timeout")], "fallback": [response()]})
    result = router.call("role", [], Output, "A2")
    assert result.success and result.provider == "fallback" and result.fallback
    assert [name for name, _ in calls] == ["primary", "fallback"]


def test_primary_retries_before_fallback(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [TimeoutError("first"), response()], "fallback": []}, retries=1)
    result = router.call("role", [], Output, "A2-retry")
    assert result.success and result.provider == "primary"
    assert [name for name, _ in calls] == ["primary", "primary"]


def test_bad_json_gets_one_format_repair(monkeypatch, tmp_path):
    router, calls = make_router(monkeypatch, tmp_path,
                                {"primary": [response("not-json"), response('{"value":"fixed"}')], "fallback": []})
    result = router.call("role", [], Output, "A3")
    assert result.data == {"value": "fixed"}
    assert len(calls) == 2


def test_no_key_and_all_models_failed(monkeypatch, tmp_path):
    router, _ = make_router(monkeypatch, tmp_path,
                            {"primary": [TimeoutError("p")], "fallback": [TimeoutError("f")]})
    monkeypatch.delenv("PRIMARY_KEY")
    result = router.call("role", [], Output, "A4")
    assert not result.success and result.data is None
    assert "not_configured" in result.error and "TimeoutError" in result.error
