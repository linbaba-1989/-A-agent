"""Role-aware OpenAI-compatible routing with bounded retry and fallback."""
from dataclasses import dataclass
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Type

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .model_registry import ModelRegistry, ProviderConfig
from .usage_tracker import UsageRecord, UsageTracker, timestamp_now


@dataclass
class RouterResult:
    role: str
    provider: str | None
    model: str | None
    success: bool
    data: dict[str, Any] | None
    latency: float
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    fallback: bool = False
    error: str | None = None
    analysis_id: str | None = None


def load_role_config(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    config_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "agent_config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    roles = payload.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ValueError("roles_config_invalid")
    for role, route in roles.items():
        if not isinstance(route, dict) or not route.get("primary") or not route.get("fallback"):
            raise ValueError(f"role_route_invalid: {role}")
    return roles


class LLMRouter:
    def __init__(self, registry: ModelRegistry | None = None, role_config: dict[str, dict[str, str]] | None = None,
                 tracker: UsageTracker | None = None, client_factory: Callable[[ProviderConfig], Any] | None = None):
        self.registry = registry or ModelRegistry()
        self.role_config = role_config or load_role_config()
        self.tracker = tracker or UsageTracker()
        self.client_factory = client_factory or (
            lambda provider: OpenAI(api_key=provider.api_key, base_url=provider.base_url,
                                    timeout=provider.timeout, max_retries=0)
        )
        self.last_results: dict[str, RouterResult] = {}
        self.role_states: dict[str, str] = {role: "idle" for role in self.role_config}
        errors = self.registry.validate()
        for role, route in self.role_config.items():
            for provider in (route["primary"], route["fallback"]):
                try:
                    self.registry.get(provider)
                except ValueError:
                    errors.append(f"unknown_provider_for_role: {role}/{provider}")
        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _content(response: Any) -> str:
        return response.choices[0].message.content

    @staticmethod
    def _tokens(response: Any) -> tuple[int, int, int]:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
        return input_tokens, output_tokens, total_tokens

    @staticmethod
    def _cost(provider: ProviderConfig, input_tokens: int, output_tokens: int) -> float:
        rates = provider.cost_profile or {}
        return (input_tokens * rates.get("input_per_million", 0.0) +
                output_tokens * rates.get("output_per_million", 0.0)) / 1_000_000

    def _request(self, provider: ProviderConfig, messages: list[dict[str, str]]) -> Any:
        client = self.client_factory(provider)
        return client.chat.completions.create(model=provider.model_name, messages=messages,
                                              response_format={"type": "json_object"}, temperature=0)

    def _attempt(self, role: str, provider: ProviderConfig, messages: list[dict[str, str]],
                 schema: Type[BaseModel], analysis_id: str, fallback: bool) -> RouterResult:
        started = perf_counter()
        response = self._request(provider, messages)
        input_tokens, output_tokens, total_tokens = self._tokens(response)
        try:
            parsed = schema.model_validate_json(self._content(response))
        except (ValidationError, json.JSONDecodeError, TypeError):
            repair = messages + [{"role": "assistant", "content": str(self._content(response))},
                                 {"role": "user", "content": "仅返回符合要求的 JSON 对象，修复格式，不增加事实。"}]
            response = self._request(provider, repair)
            extra_in, extra_out, extra_total = self._tokens(response)
            input_tokens += extra_in
            output_tokens += extra_out
            total_tokens += extra_total
            parsed = schema.model_validate_json(self._content(response))
        latency = perf_counter() - started
        cost = self._cost(provider, input_tokens, output_tokens)
        result = RouterResult(role, provider.provider_name, provider.model_name, True, parsed.model_dump(), latency,
                              input_tokens, output_tokens, total_tokens, cost, fallback, analysis_id=analysis_id)
        self.tracker.record(UsageRecord(analysis_id, provider.provider_name, provider.model_name, role,
                                        input_tokens, output_tokens, total_tokens, latency, cost,
                                        timestamp_now(), True, fallback))
        return result

    def call(self, role: str, messages: list[dict[str, str]], schema: Type[BaseModel], analysis_id: str) -> RouterResult:
        if role not in self.role_config:
            raise ValueError(f"unknown_role: {role}")
        errors = []
        self.role_states[role] = "working"
        route = self.role_config[role]
        for fallback, provider_name in enumerate((route["primary"], route["fallback"])):
            provider = self.registry.get(provider_name)
            if not provider.configured:
                errors.append(f"{provider_name}: not_configured")
                continue
            for attempt in range(provider.max_retries + 1):
                try:
                    result = self._attempt(role, provider, messages, schema, analysis_id, bool(fallback))
                    self.last_results[role] = result
                    self.role_states[role] = "fallback" if result.fallback else "idle"
                    return result
                except Exception as exc:
                    error = f"{provider_name}[{attempt + 1}]: {type(exc).__name__}: {exc}"
                    errors.append(error)
                    self.tracker.record(UsageRecord(analysis_id, provider.provider_name, provider.model_name, role,
                                                    0, 0, 0, 0.0, 0.0, timestamp_now(), False,
                                                    bool(fallback), error))
        result = RouterResult(role, None, None, False, None, 0.0, error="; ".join(errors), analysis_id=analysis_id)
        self.last_results[role] = result
        self.role_states[role] = "error"
        self.tracker.record(UsageRecord(analysis_id, "unavailable", "unavailable", role, 0, 0, 0, 0.0, 0.0,
                                        timestamp_now(), False, error=result.error))
        return result

    def healthcheck(self, provider_name: str) -> dict[str, Any]:
        provider = self.registry.get(provider_name)
        if not provider.configured:
            return {"provider": provider_name, "model": provider.model_name, "success": False,
                    "latency": 0.0, "error": "not_configured"}
        started = perf_counter()
        try:
            self._request(provider, [{"role": "user", "content": "仅返回JSON：{\"ok\":true}"}])
            return {"provider": provider_name, "model": provider.model_name, "success": True,
                    "latency": perf_counter() - started, "error": None}
        except Exception as exc:
            return {"provider": provider_name, "model": provider.model_name, "success": False,
                    "latency": perf_counter() - started, "error": f"{type(exc).__name__}: {exc}"}
