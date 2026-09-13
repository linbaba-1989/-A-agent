"""Configuration-only model registry. Importing this module never calls an API."""
from dataclasses import asdict, dataclass
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
    return {
    "openai": ProviderConfig("openai", _model("openai", "gpt-4.1-mini"), None, "OPENAI_API_KEY", priority=10,
                             cost_profile=_cost_profile("openai")),
    "deepseek": ProviderConfig("deepseek", _model("deepseek", "deepseek-chat"), "https://api.deepseek.com", "DEEPSEEK_API_KEY",
                               priority=20, cost_profile=_cost_profile("deepseek")),
    "qwen": ProviderConfig("qwen", _model("qwen", "qwen-plus"), "https://dashscope.aliyuncs.com/compatible-mode/v1",
                           "QWEN_API_KEY", priority=30, cost_profile=_cost_profile("qwen")),
    "kimi": ProviderConfig("kimi", _model("kimi", "moonshot-v1-8k"), "https://api.moonshot.cn/v1", "KIMI_API_KEY",
                           priority=40, cost_profile=_cost_profile("kimi")),
    "doubao": ProviderConfig("doubao", _model("doubao", "YOUR_DOUBAO_ENDPOINT_ID"),
                             "https://ark.cn-beijing.volces.com/api/v3", "DOUBAO_API_KEY", priority=50,
                             cost_profile=_cost_profile("doubao")),
    }


class ModelRegistry:
    def __init__(self, providers: dict[str, ProviderConfig] | None = None):
        self.providers = default_providers() if providers is None else providers

    def get(self, name: str) -> ProviderConfig:
        if name not in self.providers:
            raise ValueError(f"unknown_provider: {name}")
        return self.providers[name]

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
