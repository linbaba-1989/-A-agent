"""Compatibility entry point; new code should use LLMRouter directly."""
from pydantic import BaseModel

from .llm_router import LLMRouter


class _LegacyResponse(BaseModel):
    response: str


def ask(model_name: str, instruction: str, context: str) -> str:
    router = LLMRouter(role_config={"legacy": {"primary": model_name, "fallback": model_name}})
    result = router.call("legacy", [{"role": "system", "content": instruction},
                                    {"role": "user", "content": context}], _LegacyResponse, "legacy")
    return result.data["response"] if result.success else "unavailable"
