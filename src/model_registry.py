"""Configuration-only model registry. Importing this module never calls an API."""
from dataclasses import asdict, dataclass, replace
import os
from typing import Any


def _cost_profile(provider: str) -> dict[str, float] | None:
    prefix = provider.upper()
    input_price = os.getenv(f"{prefix}_INPUT_COST_PER_MILLION", "").strip()
    output_price = os.getenv(f"{prefix}_OUTPUT_COST_PER_MILLION", "").strip()
    if not input_price or not output_price:
        return None
    input_value, output_value = float(input_price), float(output_price)
    if input_value <= 0 or output_value <= 0:
        return None
    return {"input_per_million": input_value, "output_per_million": output_value}


def _model(provider: str, default: str) -> str:
    return os.getenv(f"{provider.upper()}_MODEL", default)


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str
    model_name: str
    base_url: str | None
    api_key_env: str
    enabled: bool = True
    timeout: float = 90.0
    max_retries: int = 0
    priority: int = 100
    cost_profile: dict[str, float] | None = None
    capabilities: tuple[str, ...] = ("chat", "json")
    supports_custom_temperature: bool = True
    fixed_temperature: float | None = None
    supports_reasoning_effort: bool = False
    supports_reasoning: bool = False
    reasoning_levels: tuple[str, ...] = ()
    reasoning_mode: str | None = None
    supports_json_schema: bool = False
    production_enabled: bool = True
    supports_structured_output: bool = True
    supports_json_object: bool = True
    supports_tools: bool | None = None
    supports_streaming: bool | None = None
    context_window: int | None = None
    pricing_status: str = "unknown"
    role_candidate_for: tuple[str, ...] = ()

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["configured"] = self.configured
        result["pricing_status"] = "configured" if self.cost_profile is not None else "unknown"
        return result


def default_providers() -> dict[str, ProviderConfig]:
    kimi_model = _model("kimi", "moonshot-v1-8k")
    return {
    "openai": ProviderConfig("openai", _model("openai", "gpt-4.1-mini"), None, "OPENAI_API_KEY",
                             enabled=os.getenv("OPENAI_ENABLED", "false").lower() == "true", priority=10,
                             cost_profile=_cost_profile("openai")),
    "deepseek": ProviderConfig("deepseek", _model("deepseek", "deepseek-v4-pro"), "https://api.deepseek.com", "DEEPSEEK_API_KEY",
                               priority=20, cost_profile=_cost_profile("deepseek"), supports_custom_temperature=False,
                               supports_reasoning_effort=True, reasoning_levels=("low", "high", "max"),
                               reasoning_mode="deepseek", supports_reasoning=True),
    "qwen": ProviderConfig("qwen", _model("qwen", "qwen3.8-max"), "https://dashscope.aliyuncs.com/compatible-mode/v1",
                           "QWEN_API_KEY", priority=30, cost_profile=_cost_profile("qwen")),
    "kimi": ProviderConfig("kimi", kimi_model, os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"), "KIMI_API_KEY",
                           priority=40, cost_profile=_cost_profile("kimi"),
                           supports_custom_temperature=kimi_model.lower() != "kimi-k3",
                           fixed_temperature=1.0 if kimi_model.lower() == "kimi-k2.6" else None,
                           reasoning_mode="provider_default" if kimi_model.lower() == "kimi-k3" else None),
    "doubao": ProviderConfig("doubao", _model("doubao", "YOUR_DOUBAO_ENDPOINT_ID"),
                             os.getenv("DOUBAO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
                             "DOUBAO_API_KEY", priority=50,
                             cost_profile=_cost_profile("doubao")),
    "glm": ProviderConfig("glm", _model("glm", ""), os.getenv("GLM_BASE_URL") or None, "GLM_API_KEY",
                          enabled=os.getenv("GLM_ENABLED", "false").lower() == "true", priority=60,
                          production_enabled=False, cost_profile=_cost_profile("glm"),
                          role_candidate_for=("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
                                              "risk_officer", "chief_researcher")),
    "hunyuan": ProviderConfig("hunyuan", _model("hunyuan", ""), os.getenv("HUNYUAN_BASE_URL") or None,
                              "HUNYUAN_API_KEY", enabled=os.getenv("HUNYUAN_ENABLED", "false").lower() == "true",
                              priority=70, production_enabled=False, cost_profile=_cost_profile("hunyuan"),
                              role_candidate_for=("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
                                                  "risk_officer", "chief_researcher")),
    "minimax": ProviderConfig("minimax", _model("minimax", ""), os.getenv("MINIMAX_BASE_URL") or None,
                              "MINIMAX_API_KEY", enabled=os.getenv("MINIMAX_ENABLED", "false").lower() == "true",
                              priority=80, production_enabled=False, cost_profile=_cost_profile("minimax"),
                              role_candidate_for=("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
                                                  "risk_officer", "chief_researcher")),
    "stepfun": ProviderConfig("stepfun", _model("stepfun", ""), os.getenv("STEPFUN_BASE_URL") or None,
                              "STEPFUN_API_KEY", enabled=os.getenv("STEPFUN_ENABLED", "false").lower() == "true",
                              priority=90, production_enabled=False, cost_profile=_cost_profile("stepfun"),
                              role_candidate_for=("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
                                                  "risk_officer", "chief_researcher")),
    }


class ModelRegistry:
    def __init__(self, providers: dict[str, ProviderConfig] | None = None):
        self.providers = default_providers() if providers is None else providers

    def get(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise ValueError(f"unknown_provider: {name}")
        return self.providers[name]

    def for_model(self, provider_name: str, model_name: str | None = None) -> ProviderConfig:
        provider = self.get(provider_name)
        model = model_name or provider.model_name
        lowered = model.lower()
        if provider_name == "deepseek" and lowered.startswith("deepseek-v4-"):
            return replace(provider, model_name=model, supports_custom_temperature=False,
                           fixed_temperature=None, supports_reasoning_effort=True,
                           reasoning_levels=("low", "high", "max"), reasoning_mode="deepseek",
                           supports_reasoning=True)
        if provider_name == "kimi" and lowered == "kimi-k3":
            return replace(provider, model_name=model, supports_custom_temperature=False,
                           fixed_temperature=None, supports_reasoning_effort=False,
                           reasoning_levels=(), reasoning_mode="provider_default")
        if provider_name == "kimi" and lowered == "kimi-k2.6":
            return replace(provider, model_name=model, supports_custom_temperature=False, fixed_temperature=1.0)
        return replace(provider, model_name=model)

    def validate(self) -> list[str]:
        errors = []
        for name, provider in self.providers.items():
            if name != provider.provider_name:
                errors.append(f"provider_name_mismatch: {name}")
            if (provider.enabled and not provider.model_name) or not provider.api_key_env or provider.timeout <= 0 or provider.max_retries < 0:
                errors.append(f"provider_config_invalid: {name}")
        return errors

    def statuses(self) -> list[dict[str, Any]]:
        return [provider.public_dict() for provider in sorted(self.providers.values(), key=lambda item: item.priority)]
