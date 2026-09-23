"""Reviewed official Chat Completions contracts; no network or env mutation."""
from dataclasses import dataclass
from inspect import signature
from urllib.parse import urlsplit
from openai.resources.chat.completions import Completions


@dataclass(frozen=True)
class OutputLimitContract:
    parameter: str
    evidence: str
    reasoning_accounting: str = "unknown"
    provider: str = ""
    official_host: str = ""
    model_pattern: str = ""  # Exact allowlisted model, never a broad prefix regex.
    api_family: str = "chat_completions"
    semantics: str = ""
    source_status: str = "official_documentation"
    includes_reasoning: bool | None = None
    output_tolerance_tokens: int | None = None
    default_completion_tokens: int | None = None
    plausible_contributor: bool = False


# Exact registrations reviewed 2026-09-23. The Ark endpoint is the existing
# configured identity, not an arbitrary ep-* match; endpoint changes fail closed.
VERIFIED_OUTPUT_LIMITS: dict[tuple[str, str, str | None], OutputLimitContract] = {}


def _register(provider, host, paths, models, field, source, semantics,
              includes_reasoning=None, tolerance=None, default=None):
    for model in models:
        contract = OutputLimitContract(field, source,
            "reasoning_plus_answer" if includes_reasoning is True else "unknown",
            provider, host, model, semantics=semantics, includes_reasoning=includes_reasoning,
            output_tolerance_tokens=tolerance, default_completion_tokens=default,
            plausible_contributor=provider == "kimi")
        for path in paths:
            VERIFIED_OUTPUT_LIMITS[(provider, model, "https://" + host + path)] = contract


_register("deepseek", "api.deepseek.com", ("", "/v1"), ("deepseek-v4-pro",),
          "max_tokens", "https://api-docs.deepseek.com/api/create-chat-completion/",
          "maximum generated completion tokens")
_register("qwen", "dashscope.aliyuncs.com", ("/compatible-mode/v1",),
          ("qwen3.8-max", "qwen3.8-max-0902"), "max_completion_tokens",
          "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions",
          "reasoning + final answer total output", True, 10)
_register("doubao", "ark.cn-beijing.volces.com", ("/api/v3",),
          ("ep-20260913102258-jq7jn",), "max_completion_tokens",
          "https://docs.volcengine.com/docs/ark/chat-api?lang=zh&redirect=1",
          "reasoning + answer total output", True)
_register("kimi", "api.moonshot.cn", ("/v1",), ("kimi-k3",),
          "max_completion_tokens", "https://platform.kimi.com/docs/api/chat",
          "maximum generated completion tokens; max_tokens is deprecated alias",
          default=131072)


class UnsupportedOutputLimit(ValueError):
    pass


def output_limit_status(provider):
    # Do not echo the supplied URL: it might contain credentials or query secrets.
    failure = {"status": "UNSUPPORTED", "parameter": None,
               "reasoning_accounting": "unknown", "evidence": None}
    try:
        url = urlsplit(provider.base_url or "")
        if (url.scheme != "https" or not url.hostname or url.username is not None or
                url.password is not None or url.query or url.fragment or url.port not in (None, 443)):
            return {**failure, "reason": "unsafe_or_non_https_endpoint"}
        base = "https://" + url.hostname + url.path.rstrip("/")
    except (ValueError, TypeError):
        return {**failure, "reason": "invalid_endpoint"}
    contracts = [c for (name, _, _), c in VERIFIED_OUTPUT_LIMITS.items() if name == provider.provider_name]
    if not any(c.official_host == url.hostname for c in contracts):
        return {**failure, "reason": "official_host_mismatch"}
    if not any(c.official_host == url.hostname and c.model_pattern == provider.model_name for c in contracts):
        return {**failure, "reason": "model_mismatch"}
    contract = VERIFIED_OUTPUT_LIMITS.get((provider.provider_name, provider.model_name, base))
    if contract is None:
        return {**failure, "reason": "api_path_mismatch"}
    if not (contract.provider == provider.provider_name and contract.official_host == url.hostname and
            contract.model_pattern == provider.model_name and contract.api_family == "chat_completions" and
            contract.source_status == "official_documentation" and contract.evidence and contract.semantics):
        return {**failure, "reason": "contract_identity_or_source_mismatch"}
    if contract.parameter not in {"max_tokens", "max_completion_tokens"} or contract.parameter not in signature(Completions.create).parameters:
        return {**failure, "reason": "sdk_parameter_unsupported"}
    return {"status": "VERIFIED", "parameter": contract.parameter,
            "provider": contract.provider, "official_host": contract.official_host,
            "model_pattern": contract.model_pattern, "api_family": contract.api_family,
            "semantics": contract.semantics, "source_status": contract.source_status,
            "reasoning_accounting": contract.reasoning_accounting, "includes_reasoning": contract.includes_reasoning,
            "output_tolerance_tokens": contract.output_tolerance_tokens,
            "default_completion_tokens": contract.default_completion_tokens,
            "plausible_contributor": contract.plausible_contributor,
            "evidence": contract.evidence}


def output_limit_kwargs(provider, max_output_tokens):
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("invalid_output_limit")
    status = output_limit_status(provider)
    if status["status"] != "VERIFIED":
        raise UnsupportedOutputLimit("provider_output_limit_unsupported")
    return {status["parameter"]: max_output_tokens}
