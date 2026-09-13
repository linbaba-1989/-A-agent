"""Configuration-only model registry. Importing this module never calls an API."""
from dataclasses import asdict, dataclass, replace
import os
from typing import Any


def _cost_profile(provider: str) -> dict[str, float]:
    prefix = provider.upper()
    return {"input_per_million": float(os.getenv(f"{prefix}_INPUT_COST_PER_MILLION", "0")),
            "output_per_million": float(os.getenv(f"{prefix}_OUTPUT_COST_PER_MILLION", "0"))}


def _model(provider: str, default: str) -> str:
    return os.getenv(f"{provider.upper()}_MODEL", default)


@dataclass(frozen=True)
class ProviderConfig:
    provider_name: str
    model_name: str
    base_url: str | None
    api_key_env: str
    enabled: bool = True
    timeout: float = 30.0
    max_retries: int = 1
    priority: int = 100
    cost_profile: dict[str, float] | None = None
    capabilities: tuple[str, ...] = ("chat", "json")
    supports_custom_temperature: bool = True
    fixed_temperature: float | None = None
    supports_reasoning_effort: bool = False
    reasoning_levels: tuple[str, ...] = ()
    reasoning_mode: str | None = None

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["configured"] = self.configured
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
                               reasoning_mode="deepseek"),
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
                           reasoning_levels=("low", "high", "max"), reasoning_mode="deepseek")
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
            if not provider.model_name or not provider.api_key_env or provider.timeout <= 0 or provider.max_retries < 0:
                errors.append(f"provider_config_invalid: {name}")
        return errors

    def statuses(self) -> list[dict[str, Any]]:
        return [provider.public_dict() for provider in sorted(self.providers.values(), key=lambda item: item.priority)]
