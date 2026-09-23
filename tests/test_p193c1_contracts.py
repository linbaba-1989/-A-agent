"""Official identity matching and request serialization; all transport is local."""
import json
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.a_share.p193_role_ab import RoleIsolatedRunner, load_fixture, build_blind_plan
from evals.a_share.runtime_guardrails import usage_accounting
from src.provider_output_limits import output_limit_status, VERIFIED_OUTPUT_LIMITS
from src.provider_adapters import OpenAICompatibleAdapter
from src.model_registry import ProviderConfig, ModelRegistry
from src.usage_tracker import UsageTracker

PROVIDERS = [
    ("deepseek", "https://api.deepseek.com", "deepseek-v4-pro", "technical_analyst", "GC01", "max_tokens", 4096),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-max", "fundamental_event_analyst", "GC04", "max_completion_tokens", 3072),
    ("doubao", "https://ark.cn-beijing.volces.com/api/v3", "ep-20260913102258-jq7jn", "sentiment_analyst", "GC03", "max_completion_tokens", 3072),
    ("kimi", "https://api.moonshot.cn/v1", "kimi-k3", "risk_officer", "GC05", "max_completion_tokens", 4096),
]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("provider_network_forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("name,url,model,role,gc,field,cap", PROVIDERS)
def test_official_contract_full_harness_to_sdk_json(name,url,model,role,gc,field,cap,monkeypatch,tmp_path):
    import httpx2
    from openai import OpenAI
    sentinel = "secret-sentinel-not-for-logging"
    monkeypatch.setenv("TEST_CONTRACT_KEY", sentinel)
    monkeypatch.setenv("DOUBAO_MODEL", PROVIDERS[2][2])
    registry = ModelRegistry({n:ProviderConfig(n,m,u,"TEST_CONTRACT_KEY") for n,u,m,*_ in PROVIDERS})
    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200,json={"choices":[{"message":{"role":"assistant","content":"{}"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":11,"completion_tokens":33,"total_tokens":44,
                     "completion_tokens_details":{"reasoning_tokens":22}}})
    clients = []
    def factory(provider):
        sdk=OpenAI(api_key=sentinel,base_url=provider.base_url,max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(respond)),timeout=provider.timeout)
        clients.append(sdk)
        return OpenAICompatibleAdapter(provider,client=sdk)
    runner=RoleIsolatedRunner(registry=registry,client_factory=factory,tracker=UsageTracker(tmp_path/'usage.jsonl'))
    result=runner.run_role(role,load_fixture(gc),few_shot_enabled=False,
        evaluation_run_id="serialization-only",anonymous_arm="X",dry_run=False,allow_network=True)
    assert result["provider_attempts"]==1 and result["runtime_diagnostics"]["response_received"]
    assert bodies[0][field]==cap
    assert set(bodies[0]) & {"max_tokens","max_completion_tokens","max_output_tokens"} == {field}
    assert result["reasoning_tokens"]==22 and result["visible_output_tokens"] is None
    assert result["runtime_diagnostics"]["timeout_config"]["read"]==(150 if name=="kimi" else 90)
    assert sentinel not in json.dumps(result)+(tmp_path/'usage.jsonl').read_text()
    # Production request without explicit cap remains unchanged.
    runner.router._request(runner._candidate(role)[1],[])
    assert not set(bodies[1]) & {"max_tokens","max_completion_tokens","max_output_tokens"}
    for client in clients: client.close()


@pytest.mark.parametrize("name,url,model,role,gc,field,cap", PROVIDERS)
def test_exact_identity_fail_closed(name,url,model,role,gc,field,cap):
    from urllib.parse import urlsplit
    provider=ProviderConfig(name,model,url,"KEY")
    status=output_limit_status(provider)
    assert status["status"]=="VERIFIED" and status["parameter"]==field
    assert status["source_status"]=="official_documentation"
    assert status["api_family"]=="chat_completions"
    assert output_limit_status(replace(provider,base_url=url+'/'))["status"]=="VERIFIED"
    host=urlsplit(url).hostname
    for endpoint in ("https://gateway.invalid/v1","https://"+host+".evil.invalid/v1",
                     url.replace("https://","http://"),url+"/responses",url+"?token=secret",
                     "https://secret@"+host,url.replace(host,host+":444"),"https://"+host+"/../v1"):
        assert output_limit_status(replace(provider,base_url=endpoint))["status"]=="UNSUPPORTED"
        assert 'secret' not in json.dumps(output_limit_status(replace(provider,base_url=endpoint)))
    assert output_limit_status(replace(provider,model_name=model+"-unknown"))["reason"]=="model_mismatch"
    assert output_limit_status(replace(provider,provider_name="unregistered"))["status"]=="UNSUPPORTED"


@pytest.mark.parametrize("blocked", ["deepseek","qwen","kimi"])
def test_no_validation_plan_with_unverified_required_provider(blocked):
    providers={n:ProviderConfig(n,m,u,"KEY") for n,u,m,*_ in PROVIDERS}
    providers[blocked]=replace(providers[blocked],base_url="https://unknown-gateway.invalid/v1")
    with pytest.raises(ValueError,match="validation_contract_unverified:"+blocked):
        build_blind_plan("blocked",registry=ModelRegistry(providers))


def test_doubao_not_required_for_six_call_plan(monkeypatch):
    monkeypatch.setenv("DOUBAO_MODEL","YOUR_DOUBAO_ENDPOINT_ID")
    result=RoleIsolatedRunner().run_round1(dry_run=True)["summary"]
    assert result["planned_base_calls"]==result["max_provider_attempts"]==6
    assert result["provider_counts"]=={"deepseek":2,"qwen":2,"kimi":2}
    assert result["actual_provider_calls"]==0


def test_qwen_snapshot_is_explicit_allowlist_not_prefix():
    p=ProviderConfig("qwen","qwen3.8-max-0902","https://dashscope.aliyuncs.com/compatible-mode/v1","KEY")
    assert output_limit_status(p)["status"]=="VERIFIED"
    assert output_limit_status(replace(p,model_name="qwen3.8-max-9999"))["status"]=="UNSUPPORTED"


def test_semantics_and_no_subtraction():
    for name,url,model,*_ in PROVIDERS:
        status=output_limit_status(ProviderConfig(name,model,url,"KEY"))
        if name in ("qwen","doubao"): assert status["includes_reasoning"] is True
        if name=="qwen": assert status["output_tolerance_tokens"]==10
        if name=="kimi":
            assert status["default_completion_tokens"]==131072
            assert status["plausible_contributor"] is True
    usage=usage_accounting({"usage":{"completion_tokens":100,"completion_tokens_details":{"reasoning_tokens":60}}})
    assert usage["visible_output_tokens"] is None
    assert usage_accounting({"usage":{"completion_tokens":100}})["reasoning_tokens"] is None


def test_duplicate_cap_rejected_without_client():
    p=ProviderConfig("kimi","kimi-k3","https://api.moonshot.cn/v1","KEY")
    adapter=OpenAICompatibleAdapter(p)
    with pytest.raises(ValueError,match="conflicting_output_limits"):
        adapter.chat_completion(max_tokens=10,max_completion_tokens=20)


@pytest.mark.parametrize("extra", [{"max_tokens":9999}, {"max_completion_tokens":9999},
                                  {"max_output_tokens":9999}, {"model":"unknown-model"}])
def test_sdk_extra_body_cannot_override_verified_contract(extra):
    p=ProviderConfig("qwen","qwen3.8-max","https://dashscope.aliyuncs.com/compatible-mode/v1","KEY")
    with pytest.raises(ValueError,match="output_contract_override_in_extra_body"):
        OpenAICompatibleAdapter(p).chat_completion(model=p.model_name,max_output_tokens=3072,extra_body=extra)


def test_explicit_cap_cannot_use_another_model():
    p=ProviderConfig("kimi","kimi-k3","https://api.moonshot.cn/v1","KEY")
    with pytest.raises(ValueError,match="output_contract_model_mismatch"):
        OpenAICompatibleAdapter(p).chat_completion(model="different-model",max_output_tokens=4096)
