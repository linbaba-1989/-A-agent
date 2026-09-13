"""Role-aware OpenAI-compatible routing with bounded retry and fallback."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

from .model_registry import ModelRegistry, ProviderConfig
from .provider_adapters import ADAPTERS, OpenAICompatibleAdapter
from .analysis_profiles import get_profile
from .usage_tracker import UsageRecord, UsageTracker, timestamp_now


@dataclass
class RouterResult:
    role: str
    provider: str | None
    model: str | None
    success: bool
    data: dict[str, Any] | None
    latency: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    cost_status: str = "unknown"
    fallback: bool = False
    error: str | None = None
    analysis_id: str | None = None
    analysis_mode: str = "standard"
    reasoning_effort: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    fallback_reason: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    status: str | None = None
    usage_status: str = "unavailable"
    schema_repair_count: int = 0
    connect_latency: float | None = None
    first_token_latency: float | None = None
    timeout_stage: str | None = None


class AttemptFailure(Exception):
    def __init__(self, message: str, *, usage: tuple[int, int, int] | None = None,
                 latency: float = 0.0, repair_count: int = 0, timeout_stage: str | None = None):
        super().__init__(message)
        self.usage = usage
        self.latency = latency
        self.repair_count = repair_count
        self.timeout_stage = timeout_stage


def load_role_config(path: str | Path | None = None) -> dict[str, dict[str, str]]:
    config_path = Path(path) if path else Path(__file__).resolve().parents[1] / "config" / "agent_config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    roles = payload.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ValueError("roles_config_invalid")
    for role, route in roles.items():
        legacy = isinstance(route, dict) and route.get("primary") and route.get("fallback")
        candidates = isinstance(route, dict) and isinstance(route.get("candidates"), list) and route["candidates"]
        if not legacy and not candidates:
            raise ValueError(f"role_route_invalid: {role}")
    return roles


class LLMRouter:
    def __init__(self, registry: ModelRegistry | None = None, role_config: dict[str, dict[str, str]] | None = None,
                 tracker: UsageTracker | None = None, client_factory: Callable[[ProviderConfig], Any] | None = None):
        self.registry = registry or ModelRegistry()
        self.role_config = role_config or load_role_config()
        self.tracker = tracker or UsageTracker()
        self.client_factory = client_factory or (
            lambda provider: ADAPTERS.get(provider.provider_name, OpenAICompatibleAdapter)(provider)
        )
        self.last_results: dict[str, RouterResult] = {}
        self.role_states: dict[str, str] = {role: "idle" for role in self.role_config}
        errors = self.registry.validate()
        for role in self.role_config:
            for candidate in self._candidates(role):
                try:
                    self.registry.get(candidate["provider"])
                except ValueError:
                    errors.append(f"unknown_provider_for_role: {role}/{candidate['provider']}")
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
    def _cost(provider: ProviderConfig, input_tokens: int, output_tokens: int) -> tuple[float | None, str]:
        rates = provider.cost_profile
        if not rates or "input_per_million" not in rates or "output_per_million" not in rates:
            return None, "unknown"
        return ((input_tokens * rates["input_per_million"] +
                 output_tokens * rates["output_per_million"]) / 1_000_000, "estimated")

    def _candidates(self, role: str) -> list[dict[str, Any]]:
        route = self.role_config[role]
        if "candidates" in route:
            result = []
            for candidate in route["candidates"]:
                item = dict(candidate)
                if item.get("model_env"):
                    item["model"] = os.getenv(item["model_env"]) or self.registry.get(item["provider"]).model_name
                result.append(item)
            return result
        return [{"provider": route["primary"]}, {"provider": route["fallback"]}]

    @staticmethod
    def _schema_messages(messages: list[dict[str, str]], schema: Type[BaseModel]) -> list[dict[str, str]]:
        instruction = ("只允许返回符合以下 JSON Schema 的 JSON；禁止增加字段，禁止改变字段类型，"
                       "禁止 markdown，禁止解释文字。\nJSON Schema:\n" +
                       json.dumps(schema.model_json_schema(), ensure_ascii=False))
        return [*messages, {"role": "system", "content": instruction}]

    def _request(self, provider: ProviderConfig, messages: list[dict[str, str]],
                 reasoning_effort: str | None = None, schema: Type[BaseModel] | None = None) -> Any:
        client = self.client_factory(provider)
        response_format: dict[str, Any] = {"type": "json_object"}
        if schema is not None and provider.supports_json_schema:
            response_format = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": schema.model_json_schema()}}
        kwargs: dict[str, Any] = {"model": provider.model_name, "messages": messages,
                                  "response_format": response_format}
        if provider.fixed_temperature is not None:
            kwargs["temperature"] = provider.fixed_temperature
        elif provider.supports_custom_temperature:
            kwargs["temperature"] = 0
        if reasoning_effort and provider.supports_reasoning_effort:
            if reasoning_effort not in provider.reasoning_levels:
                raise ValueError(f"unsupported_reasoning_effort: {provider.provider_name}/{reasoning_effort}")
            kwargs["reasoning_effort"] = reasoning_effort
            if provider.reasoning_mode == "deepseek":
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if hasattr(client, "chat_completion"):
            return client.chat_completion(**kwargs)
        return client.chat.completions.create(**kwargs)

    def _attempt(self, role: str, provider: ProviderConfig, messages: list[dict[str, str]],
                 schema: Type[BaseModel], analysis_id: str, fallback: bool, analysis_mode: str,
                 reasoning_effort: str | None, requested_model: str, fallback_reason: str | None) -> RouterResult:
        started = perf_counter()
        constrained_messages = self._schema_messages(messages, schema)
        try:
            response = self._request(provider, constrained_messages, reasoning_effort, schema)
        except TimeoutError as exc:
            raise AttemptFailure(f"TimeoutError: {exc}", latency=perf_counter() - started,
                                 timeout_stage="response") from exc
        input_tokens, output_tokens, total_tokens = self._tokens(response)
        repair_count = 0
        try:
            parsed = schema.model_validate_json(self._content(response))
        except (ValidationError, json.JSONDecodeError, TypeError) as validation_error:
            repair_count = 1
            repair = constrained_messages + [
                {"role": "assistant", "content": str(self._content(response))},
                {"role": "user", "content": (
                    "只修复结构，不得增加新的事实或分析内容。只返回 JSON。\n"
                    f"Validation errors:\n{validation_error}\nCorrect JSON Schema:\n"
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False))}]
            try:
                response = self._request(provider, repair, reasoning_effort, schema)
            except TimeoutError as exc:
                raise AttemptFailure(f"schema_repair_timeout: {exc}",
                                     usage=(input_tokens, output_tokens, total_tokens),
                                     latency=perf_counter() - started, repair_count=1,
                                     timeout_stage="schema_repair") from exc
            extra_in, extra_out, extra_total = self._tokens(response)
            input_tokens += extra_in
            output_tokens += extra_out
            total_tokens += extra_total
            try:
                parsed = schema.model_validate_json(self._content(response))
            except (ValidationError, json.JSONDecodeError, TypeError) as exc:
                raise AttemptFailure("schema_validation_failed",
                                     usage=(input_tokens, output_tokens, total_tokens),
                                     latency=perf_counter() - started, repair_count=1) from exc
        latency = perf_counter() - started
        cost, cost_status = self._cost(provider, input_tokens, output_tokens)
        result = RouterResult(role, provider.provider_name, provider.model_name, True, parsed.model_dump(), latency,
                              input_tokens, output_tokens, total_tokens, cost, cost_status, fallback, analysis_id=analysis_id,
                              analysis_mode=analysis_mode, reasoning_effort=reasoning_effort,
                              requested_model=requested_model, actual_model=provider.model_name,
                              fallback_reason=fallback_reason, usage_status="available",
                              schema_repair_count=repair_count)
        self.tracker.record(UsageRecord(analysis_id, provider.provider_name, provider.model_name, role,
                                        input_tokens, output_tokens, total_tokens, latency, cost,
                                        timestamp_now(), True, fallback, analysis_mode=analysis_mode, cost_status=cost_status,
                                        reasoning_effort=reasoning_effort, requested_model=requested_model,
                                        actual_model=provider.model_name, fallback_reason=fallback_reason,
                                        usage_status="available", schema_repair_count=repair_count))
        return result

    def call(self, role: str, messages: list[dict[str, str]], schema: Type[BaseModel], analysis_id: str,
             analysis_mode: str = "standard") -> RouterResult:
        if role not in self.role_config:
            raise ValueError(f"unknown_role: {role}")
        get_profile(analysis_mode)
        errors = []
        self.role_states[role] = "working"
        candidates = self._candidates(role)
        requested_model = candidates[0].get("model") or self.registry.get(candidates[0]["provider"]).model_name
        for index, candidate in enumerate(candidates):
            provider_name = candidate["provider"]
            provider = self.registry.for_model(provider_name, candidate.get("model"))
            provider = replace(provider, timeout=max(provider.timeout, 180.0 if analysis_mode == "max" else 90.0),
                               max_retries=0)
            fallback = index > 0
            reasoning_effort = (candidate.get("reasoning") or {}).get(analysis_mode)
            fallback_reason = "; ".join(errors) if fallback and errors else None
            if not provider.configured:
                errors.append(f"{provider_name}: not_configured")
                continue
            for attempt in range(1):
                try:
                    result = self._attempt(role, provider, messages, schema, analysis_id, fallback, analysis_mode,
                                           reasoning_effort, requested_model, fallback_reason)
                    self.last_results[role] = result
                    self.role_states[role] = "fallback" if result.fallback else "idle"
                    return result
                except Exception as exc:
                    error = f"{provider_name}[{attempt + 1}]: {type(exc).__name__}: {exc}"
                    errors.append(error)
                    failure_usage = exc.usage if isinstance(exc, AttemptFailure) else None
                    failure_latency = exc.latency if isinstance(exc, AttemptFailure) else 0.0
                    repair_count = exc.repair_count if isinstance(exc, AttemptFailure) else 0
                    timeout_stage = exc.timeout_stage if isinstance(exc, AttemptFailure) else (
                        "response" if isinstance(exc, TimeoutError) else None)
                    usage_status = "available" if failure_usage is not None else "unavailable"
                    tokens = failure_usage or (None, None, None)
                    failure_cost, failure_cost_status = (self._cost(provider, *(tokens[:2])) if failure_usage
                                                        else (None, "unknown"))
                    self.tracker.record(UsageRecord(analysis_id, provider.provider_name, provider.model_name, role,
                                                    *tokens, failure_latency, failure_cost,
                                                    timestamp_now(), False,
                                                    fallback, error, analysis_mode=analysis_mode,
                                                    reasoning_effort=reasoning_effort, requested_model=requested_model,
                                                    actual_model=provider.model_name, fallback_reason=fallback_reason,
                                                    usage_status=usage_status, schema_repair_count=repair_count,
                                                    timeout_stage=timeout_stage, cost_status=failure_cost_status))
        result = RouterResult(role, None, None, False, None, 0.0, error="; ".join(errors), analysis_id=analysis_id,
                              analysis_mode=analysis_mode, requested_model=requested_model,
                              fallback_reason="; ".join(errors), usage_status="unavailable")
        self.last_results[role] = result
        self.role_states[role] = "error"
        self.tracker.record(UsageRecord(analysis_id, "unavailable", "unavailable", role, None, None, None, 0.0, None,
                                        timestamp_now(), False, error=result.error, analysis_mode=analysis_mode,
                                        requested_model=requested_model, fallback_reason=result.fallback_reason,
                                        usage_status="unavailable", cost_status="unknown"))
        return result

    def healthcheck(self, provider_name: str, model_name: str | None = None,
                    reasoning_effort: str | None = None) -> dict[str, Any]:
        provider = self.registry.for_model(provider_name, model_name)
        if not provider.configured:
            return {"provider": provider_name, "model": provider.model_name, "success": False,
                    "latency": 0.0, "error": "not_configured"}
        started = perf_counter()
        try:
            self._request(provider, [{"role": "user", "content": "仅返回JSON：{\"ok\":true}"}], reasoning_effort)
            return {"provider": provider_name, "model": provider.model_name, "success": True,
                    "latency": perf_counter() - started, "error": None}
        except Exception as exc:
            return {"provider": provider_name, "model": provider.model_name, "success": False,
                    "latency": perf_counter() - started, "error": f"{type(exc).__name__}: {exc}"}
