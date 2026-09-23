"""Eval-only, allowlisted diagnostics; never serialize exception text or headers."""
from datetime import datetime, timezone

OUTPUT_CAPS = {"technical_analyst": 4096, "fundamental_event_analyst": 3072,
               "sentiment_analyst": 3072, "risk_officer": 4096, "chief_researcher": 6144}


def now():
    return datetime.now(timezone.utc).isoformat()


def field(value, key):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def count(value):
    return value if type(value) is int and value >= 0 else None


def usage_accounting(response):
    usage = field(response, "usage")
    details = field(usage, "completion_tokens_details")
    result = {"input_tokens": count(field(usage, "prompt_tokens")),
              "output_tokens": count(field(usage, "completion_tokens")),
              "total_tokens": count(field(usage, "total_tokens")),
              "reasoning_tokens": count(field(details, "reasoning_tokens")),
              "visible_output_tokens": count(field(details, "text_tokens"))}
    split = any(result[k] is not None for k in ("reasoning_tokens", "visible_output_tokens"))
    result["usage_accounting_status"] = ("provider_reported_split" if split else
        "provider_reported_combined" if result["output_tokens"] is not None else "unknown")
    return result


def timeout_phase(exc):
    # Examine types, never messages. A read timeout does not prove generation.
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        for module in ("httpx2", "httpx"):
            try:
                transport = __import__(module)
            except ImportError:
                continue
            for name, phase in (("ConnectTimeout", "connect"), ("ReadTimeout", "read"),
                                ("WriteTimeout", "write"), ("PoolTimeout", "pool")):
                if isinstance(exc, getattr(transport, name)):
                    return phase
        exc = exc.__cause__
    return "unknown"


def diagnostics(provider, role, cap):
    return {"request_started_at": None, "request_finished_at": None,
            "elapsed_seconds": None, "timeout_config": {
                key: provider.timeout for key in ("connect", "read", "write", "pool")},
            "exception_type": None, "provider": provider.provider_name,
            "model": provider.model_name, "role": role, "max_output_tokens": cap,
            "response_received": False, "timeout_phase": "unknown"}
