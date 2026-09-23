"""Provider adapters used by the router and Model Arena.

Constructing adapters is side-effect free. Network access occurs only through an
explicit completion or health-check call.
"""
from typing import Any, Type

from openai import OpenAI
from pydantic import BaseModel

from .model_registry import ProviderConfig
from .provider_output_limits import output_limit_kwargs


class OpenAICompatibleAdapter:
    def __init__(self, config: ProviderConfig, client: Any | None = None):
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url,
                                  timeout=self.config.timeout, max_retries=0)
        return self._client

    @staticmethod
    def output_budget(config: ProviderConfig, max_output_tokens: int) -> dict[str, int]:
        return output_limit_kwargs(config, max_output_tokens)

    def chat_completion(self, *, max_output_tokens: int | None = None, **kwargs: Any) -> Any:
        if "max_tokens" in kwargs and "max_completion_tokens" in kwargs:
            raise ValueError("conflicting_output_limits")
        cap_fields = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
        if max_output_tokens is not None or cap_fields.intersection(kwargs):
            extra = kwargs.get("extra_body") or {}
            if cap_fields.union({"model"}).intersection(extra):
                raise ValueError("output_contract_override_in_extra_body")
            if kwargs.get("model") != self.config.model_name:
                raise ValueError("output_contract_model_mismatch")
        if max_output_tokens is not None:
            if any(key in kwargs for key in ("max_tokens", "max_completion_tokens")):
                raise ValueError("conflicting_output_limits")
            kwargs.update(self.output_budget(self.config, max_output_tokens))
        return self.client.chat.completions.create(**kwargs)

    def structured_completion(self, schema: Type[BaseModel], **kwargs: Any) -> Any:
        if self.config.supports_json_schema:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": schema.model_json_schema()}}
        else:
            kwargs["response_format"] = {"type": "json_object"}
        return self.chat_completion(**kwargs)

    def health_check(self) -> dict[str, Any]:
        if not self.config.configured:
            return {"success": False, "error": "not_configured"}
        try:
            self.chat_completion(model=self.config.model_name,
                                 messages=[{"role": "user", "content": "仅返回JSON：{\"ok\":true}"}],
                                 response_format={"type": "json_object"})
            return {"success": True, "error": None}
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__}

    def supports_json_schema(self) -> bool:
        return self.config.supports_json_schema

    def supports_json_object(self) -> bool:
        return self.config.supports_json_object

    def supports_tools(self) -> bool | None:
        return self.config.supports_tools

    @staticmethod
    def get_usage(response: Any) -> dict[str, int | None]:
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        total = getattr(usage, "total_tokens", None)
        return {"input_tokens": prompt, "output_tokens": completion, "total_tokens": total}


class GLMAdapter(OpenAICompatibleAdapter):
    pass


class HunyuanAdapter(OpenAICompatibleAdapter):
    pass


class MiniMaxAdapter(OpenAICompatibleAdapter):
    pass


class StepFunAdapter(OpenAICompatibleAdapter):
    pass


ADAPTERS = {"glm": GLMAdapter, "hunyuan": HunyuanAdapter,
            "minimax": MiniMaxAdapter, "stepfun": StepFunAdapter}
