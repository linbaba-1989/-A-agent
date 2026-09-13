from src.model_registry import ModelRegistry, ProviderConfig
from src.llm_router import load_role_config


def test_registry_contains_production_and_candidate_providers_without_api_calls(monkeypatch):
    for name in ("OPENAI", "DEEPSEEK", "QWEN", "KIMI", "DOUBAO", "GLM", "HUNYUAN", "MINIMAX", "STEPFUN"):
        monkeypatch.delenv(f"{name}_API_KEY", raising=False)
        monkeypatch.delenv(f"{name}_ENABLED", raising=False)
    registry = ModelRegistry()
    assert set(registry.providers) == {"openai", "deepseek", "qwen", "kimi", "doubao",
                                      "glm", "hunyuan", "minimax", "stepfun"}
    assert registry.validate() == []
    assert all(not row["configured"] for row in registry.statuses())
    assert all("api_key" not in row for row in registry.statuses())
    assert all(not registry.get(name).production_enabled for name in ("glm", "hunyuan", "minimax", "stepfun"))


def test_candidate_models_are_config_driven_and_unknown_capabilities_stay_unknown(monkeypatch):
    monkeypatch.setenv("GLM_MODEL", "local-candidate-id")
    registry = ModelRegistry()
    glm = registry.get("glm")
    assert glm.model_name == "local-candidate-id"
    assert glm.supports_tools is None and glm.supports_streaming is None and glm.context_window is None
    assert glm.pricing_status == "unknown"


def test_zero_placeholder_prices_remain_unknown(monkeypatch):
    monkeypatch.setenv("GLM_INPUT_COST_PER_MILLION", "0")
    monkeypatch.setenv("GLM_OUTPUT_COST_PER_MILLION", "0")
    assert ModelRegistry().get("glm").cost_profile is None


def test_registry_reports_invalid_config():
    registry = ModelRegistry({"wrong": ProviderConfig("actual", "", None, "KEY", timeout=0, max_retries=-1)})
    assert registry.validate()


def test_runtime_environment_is_read_when_registry_is_created(monkeypatch):
    monkeypatch.setenv("DOUBAO_MODEL", "deployment-from-env")
    monkeypatch.setenv("OPENAI_INPUT_COST_PER_MILLION", "1.25")
    monkeypatch.setenv("OPENAI_OUTPUT_COST_PER_MILLION", "2.50")
    registry = ModelRegistry()
    assert registry.get("doubao").model_name == "deployment-from-env"
    assert registry.get("openai").cost_profile["input_per_million"] == 1.25


def test_kimi_k26_declares_fixed_temperature(monkeypatch):
    monkeypatch.setenv("KIMI_MODEL", "kimi-k2.6")
    registry = ModelRegistry()
    assert registry.get("kimi").fixed_temperature == 1.0
    assert registry.get("deepseek").fixed_temperature is None
    assert registry.get("qwen").fixed_temperature is None


def test_role_routes_are_configuration_driven():
    roles = load_role_config()
    assert roles["technical_analyst"]["candidates"][0]["model"] == "deepseek-v4-pro"
    assert roles["fundamental_event_analyst"]["candidates"][0]["model"] == "qwen3.8-max"
    assert roles["sentiment_analyst"]["candidates"][0]["provider"] == "doubao"
    assert roles["risk_officer"]["candidates"][0] == {"provider": "kimi", "model": "kimi-k3"}
    assert roles["chief_researcher"]["candidates"][0]["provider"] == "deepseek"
    assert all(candidate["provider"] != "openai" for route in roles.values() for candidate in route["candidates"])


def test_openai_disabled_and_doubao_endpoint_configurable(monkeypatch):
    monkeypatch.setenv("DOUBAO_MODEL", "ep-new")
    monkeypatch.setenv("DOUBAO_BASE_URL", "https://example.invalid/v3")
    registry = ModelRegistry()
    assert not registry.get("openai").enabled
    assert registry.get("doubao").model_name == "ep-new"
    assert registry.get("doubao").base_url == "https://example.invalid/v3"
