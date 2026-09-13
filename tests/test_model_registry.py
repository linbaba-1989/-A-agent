from src.model_registry import ModelRegistry, ProviderConfig
from src.llm_router import load_role_config


def test_registry_contains_five_providers_and_does_not_call_api(monkeypatch):
    for name in ("OPENAI", "DEEPSEEK", "QWEN", "KIMI", "DOUBAO"):
        monkeypatch.delenv(f"{name}_API_KEY", raising=False)
    registry = ModelRegistry()
    assert set(registry.providers) == {"openai", "deepseek", "qwen", "kimi", "doubao"}
    assert registry.validate() == []
    assert all(not row["configured"] for row in registry.statuses())
    assert all("api_key" not in row for row in registry.statuses())


def test_registry_reports_invalid_config():
    registry = ModelRegistry({"wrong": ProviderConfig("actual", "", None, "KEY", timeout=0, max_retries=-1)})
    assert registry.validate()


def test_runtime_environment_is_read_when_registry_is_created(monkeypatch):
    monkeypatch.setenv("DOUBAO_MODEL", "deployment-from-env")
    monkeypatch.setenv("OPENAI_INPUT_COST_PER_MILLION", "1.25")
    registry = ModelRegistry()
    assert registry.get("doubao").model_name == "deployment-from-env"
    assert registry.get("openai").cost_profile["input_per_million"] == 1.25


def test_role_routes_are_configuration_driven():
    roles = load_role_config()
    assert roles == {
        "technical_analyst": {"primary": "deepseek", "fallback": "openai"},
        "fundamental_event_analyst": {"primary": "qwen", "fallback": "kimi"},
        "sentiment_analyst": {"primary": "doubao", "fallback": "kimi"},
        "risk_officer": {"primary": "openai", "fallback": "deepseek"},
        "chief_researcher": {"primary": "openai", "fallback": "deepseek"},
    }
