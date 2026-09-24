"""P1.9.3A role-isolated A/B harness.

This module is evaluation-only.  It reuses production prompt, schema, provider
mapping, adapter and retrieval code, but deliberately bypasses the production
``StockResearchAgent.analyze`` fan-out.  A live run is opt-in and makes exactly
one provider attempt per planned role; fallback and model schema repair are
disabled so a provider or raw-schema failure is visible to the evaluator.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from src.a_share_factors import confidence_cap
from src.agent import ROLE_SCHEMAS, StockResearchAgent
from src.agent_schemas import ChiefReport
from src.few_shot import FewShotRetriever, estimate_tokens
from src.llm_router import LLMRouter, load_role_config
from src.model_arena import stable_fact_bundle_hash
from src.model_registry import ModelRegistry, ProviderConfig
from src.provider_adapters import ADAPTERS, OpenAICompatibleAdapter
from src.provider_output_limits import output_limit_status
from evals.a_share.runtime_guardrails import OUTPUT_CAPS, diagnostics, now, timeout_phase, usage_accounting
from evals.a_share.p193f_fact_relations import derive_fact_relations, semantic_relation_violations, unavailable_evidence_hits, trade_instruction_hits
from src.usage_tracker import UsageRecord, UsageTracker, timestamp_now


GOLDEN_ROOT = ROOT / "evals" / "a_share" / "golden" / "p193"
BUNDLE_PATH = ROOT / "evals" / "a_share" / "p193_synthetic_specialist_bundle.json"

ROLE_SCHEMAS_WITH_CHIEF = {**ROLE_SCHEMAS, "chief_researcher": ChiefReport}
SPECIALIST_ROLES = tuple(ROLE_SCHEMAS)
ALL_ROLES = (*SPECIALIST_ROLES, "chief_researcher")
FIXTURES = {
    "GC01": GOLDEN_ROOT / "gc01_short_weak_mid_strong.json",
    "GC02": GOLDEN_ROOT / "gc02_breakout_unconfirmed.json",
    "GC03": GOLDEN_ROOT / "gc03_high_turnover_market_data_missing.json",
    "GC04": GOLDEN_ROOT / "gc04_fundamental_event_data_missing.json",
    "GC05": GOLDEN_ROOT / "gc05_tech_bullish_risk_high.json",
}
ROUND1_PAIRS = (
    ("GC01", "technical_analyst"),
    ("GC04", "fundamental_event_analyst"),
    ("GC03", "sentiment_analyst"),
    ("GC05", "risk_officer"),
    ("GC05", "chief_researcher"),
)
NEXT_VALIDATION_PAIRS = (
    ("GC01", "technical_analyst"),
    ("GC04", "fundamental_event_analyst"),
    ("GC05", "risk_officer"),
)
BASELINE_SMOKE_PAIRS = (
    ("GC01", "technical_analyst"),
    ("GC04", "fundamental_event_analyst"),
    ("GC05", "risk_officer"),
)
BASELINE_RECHECK_PAIRS = (
    ("GC01", "technical_analyst"),
    ("GC05", "risk_officer"),
)
# Native contracts (direct endpoints, not hosted substitutes):
# https://api-docs.deepseek.com/api/create-chat-completion/
# https://help.aliyun.com/en/model-studio/qwen-structured-output
# https://platform.kimi.com/docs/guide/use-json-mode-feature-of-kimi-api
# https://platform.kimi.com/docs/guide/use-reasoning-effort
EVAL_NATIVE_CONTRACTS = {
    "technical_analyst": ("deepseek", "deepseek-v4-pro", 90.0, "json_object"),
    "fundamental_event_analyst": ("qwen", "qwen3.8-max", 90.0, "json_schema"),
    "risk_officer": ("kimi", "kimi-k3", 150.0, "json_object"),
}
STRUCTURED_OUTPUT_DISCIPLINE = (
    "STRUCTURED_OUTPUT_DISCIPLINE：只返回符合给定 Schema 的最终 JSON 对象；"
    "覆盖既有 Schema 字段，不输出 Markdown、代码块、JSON 之外的解释或分析过程。"
    "每个 summary 保持简洁，evidence 只列必要证据，drivers 与 risks 不重复；"
    "missing evidence 简短列明缺口，不复述完整 Fact Bundle 或 Few-shot 案例。"
)
FACT_SEMANTIC_DISCIPLINE = """FACT SEMANTIC DISCIPLINE
盘中intraday snapshot不得描述为close/收盘；区间位置range position不是统计percentile。
没有均线历史或slope不得描述均线斜率、拐头、走平或持续变化。
条件语句必须明确使用若/如果/后续；未来失效条件不得写成当前事实。
不得重新计算覆盖derived_fact_relations；这些是事实边界，不是方向结论。
"""

FACT_RELATION_DISCIPLINE = (
    "FACT RELATION DISCIPLINE：derived_fact_relations 由当前只读 Fact Bundle 确定性计算，"
    "不得重新计算、覆盖或与其冲突。若 price_vs_10d_high=below，不得声称现价已突破10日高点；"
    "若 snapshot_type=intraday 且 is_market_close=false，不得使用收盘、收于、日终确认描述当前快照。"
    "合成评测样本只能引用 allowed_source_labels 和输入中明确提供的来源；"
    "不得自行标注 QMT、Wind、同花顺、财联社或公司公告等未提供的来源。"
)
OUTPUT_TOKEN_ESTIMATES = {
    "technical_analyst": 200,
    "fundamental_event_analyst": 120,
    "sentiment_analyst": 130,
    "risk_officer": 170,
    "chief_researcher": 550,
}


def bootstrap_eval_environment() -> None:
    """Use production's dotenv semantics with a path stable across working directories."""
    load_dotenv(dotenv_path=ROOT / ".env", override=False)


def _config_status(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "missing"
    if any(marker in value.upper() for marker in ("YOUR_", "PLACEHOLDER", "REPLACE_ME", "PASTE_")):
        return "placeholder"
    return "configured"


def _endpoint_status(value: str | None) -> str:
    status = _config_status(value)
    if status != "configured":
        return status
    parsed = urlparse(value)
    return "configured" if parsed.scheme in ("http", "https") and parsed.netloc else "invalid"


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def deterministic_enabled_order(seed: str, case_id: str, role: str) -> tuple[bool, bool]:
    """Reproducible order with no fixed X assignment for the enabled arm."""
    digest = sha256(f"{seed}:{case_id}:{role}".encode("utf-8")).digest()
    return (False, True) if digest[0] % 2 == 0 else (True, False)


def baseline_reliability_gate(
    results: list[dict[str, Any]], manual_fact_review: dict[str, bool] | None = None
) -> dict[str, Any]:
    """Require three valid OFF baselines and a factual review before live A/B."""
    if not results:
        return {"status": "NOT_RUN", "failures": [],
                "review_required": [role for _, role in BASELINE_SMOKE_PAIRS]}
    failures: list[str] = []
    expected = {(case_id, role) for case_id, role in BASELINE_SMOKE_PAIRS}
    observed: set[tuple[str, str]] = set()
    for result in results:
        case_id = str(result.get("case_id", "")).split("_", 1)[0]
        role = result.get("role")
        pair = (case_id, role)
        if pair not in expected or pair in observed:
            failures.append("unexpected_or_duplicate_baseline")
            continue
        observed.add(pair)
        fixture = load_fixture(case_id)
        if result.get("fixture_hash") != fixture["fixture_hash"]:
            failures.append(f"{role}:fixture_hash_mismatch")
        if result.get("fact_bundle_hash") != fixture["fact_bundle_hash"]:
            failures.append(f"{role}:fact_bundle_hash_mismatch")
        contract = EVAL_NATIVE_CONTRACTS[role]
        if result.get("provider") != contract[0] or result.get("model") != contract[1]:
            failures.append(f"{role}:provider_model_mismatch")
        expected_effort = "none" if role == "technical_analyst" else "low"
        if (result.get("reasoning_effort") != expected_effort or
                result.get("response_format_type") != contract[3] or
                result.get("runtime_diagnostics", {}).get("timeout_config", {}).get("read") != contract[2] or
                result.get("max_output_tokens") != OUTPUT_CAPS[role] or
                result.get("schema_hash") != canonical_hash(ROLE_SCHEMAS_WITH_CHIEF[role].model_json_schema())):
            failures.append(f"{role}:eval_contract_mismatch")
        if result.get("few_shot_enabled") is not False:
            failures.append(f"{role}:baseline_not_off")
        if (result.get("provider_attempts") != 1 or result.get("fallback_used") is not False or
                result.get("repair_used") is not False):
            failures.append(f"{role}:attempt_policy_failed")
        if (result.get("status") not in {"PASS", "SEMANTIC_FAIL"} or result.get("schema_valid") is not True or
                result.get("finish_reason") != "stop"):
            failures.append(f"{role}:response_or_schema_failed")
        if not result.get("runtime_diagnostics", {}).get("response_received"):
            failures.append(f"{role}:no_provider_response")
        metrics = result.get("metrics") or {}
        if isinstance(result.get("parsed_response"), dict):
            metrics = {**metrics, **semantic_relation_violations(result["parsed_response"], fixture["facts"])}
        if metrics.get("hallucination_rule_hits") or metrics.get("confidence_cap") is not True:
            failures.append(f"{role}:fact_or_confidence_rule_failed")
        if metrics.get("unavailable_to_confirmed"):
            failures.append(f"{role}:unavailable_to_confirmed")
        if any(
            metrics.get(name, False) is not False for name in (
                "numeric_relation_violation", "temporal_semantics_violation", "provenance_violation",
                "semantic_transformation_violation", "unsupported_trajectory_claim", "trade_instruction_violation")
        ):
            failures.append(f"{role}:semantic_fact_relation_failed")
    if observed != expected:
        failures.append("incomplete_baseline_set")
    review_required = [role for _, role in BASELINE_SMOKE_PAIRS
                       if manual_fact_review is None or manual_fact_review.get(role) is not True]
    if manual_fact_review is not None and any(manual_fact_review.get(role) is False
                                              for _, role in BASELINE_SMOKE_PAIRS):
        failures.append("manual_fact_review_failed")
    status = "FAIL" if failures else "PENDING_FACT_REVIEW" if review_required else "PASS"
    return {"status": status, "failures": failures, "review_required": review_required}


def load_fixture(case_id: str) -> dict[str, Any]:
    fixture = json.loads(FIXTURES[case_id].read_text(encoding="utf-8"))
    expected = fixture["fixture_hash"]
    actual = canonical_hash({key: value for key, value in fixture.items() if key != "fixture_hash"})
    if expected != actual:
        raise ValueError(f"fixture_hash_mismatch:{case_id}")
    return fixture


def load_synthetic_specialist_bundle() -> dict[str, Any]:
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    expected = bundle["bundle_hash"]
    actual = canonical_hash({key: value for key, value in bundle.items() if key != "bundle_hash"})
    if expected != actual:
        raise ValueError("synthetic_specialist_bundle_hash_mismatch")
    if bundle.get("input_classification") != "synthetic_eval_input":
        raise ValueError("synthetic_specialist_bundle_classification_missing")
    if bundle.get("not_model_output") is not True or bundle.get("not_golden_fact_bundle") is not True:
        raise ValueError("synthetic_specialist_bundle_boundary_missing")
    return bundle


@dataclass(frozen=True)
class BlindPlan:
    evaluation_run_id: str
    seed: str
    public_tasks: tuple[dict[str, Any], ...]
    private_mapping: dict[str, bool]

    def public_dict(self) -> dict[str, Any]:
        return {"evaluation_run_id": self.evaluation_run_id, "seed": self.seed,
                "tasks": list(self.public_tasks)}


def build_blind_plan(evaluation_run_id: str, role_config: dict[str, Any] | None = None,
                     registry: ModelRegistry | None = None) -> BlindPlan:
    """Build a reproducible X/Y order without exposing the OFF/ON mapping."""
    bootstrap_eval_environment()
    config = role_config or load_role_config()
    model_registry = registry or ModelRegistry()
    seed = sha256(f"p193a:{evaluation_run_id}".encode("utf-8")).hexdigest()
    tasks: list[dict[str, Any]] = []
    mapping: dict[str, bool] = {}
    for case_id, role in NEXT_VALIDATION_PAIRS:
        fixture = load_fixture(case_id)
        route = config[role]
        candidate = dict(route["candidates"][0]) if "candidates" in route else {"provider": route["primary"]}
        provider = candidate["provider"]
        model = candidate.get("model") or model_registry.get(provider).model_name
        if candidate.get("model_env"):
            model = os.getenv(candidate["model_env"]) or model_registry.get(provider).model_name
        resolved = model_registry.for_model(provider, model)
        cap_status = output_limit_status(resolved)
        if cap_status["status"] != "VERIFIED":
            raise ValueError("validation_contract_unverified:" + provider + ":" + cap_status["reason"])
        enabled_order = deterministic_enabled_order(seed, case_id, role)
        for sequence, enabled in enumerate(enabled_order):
            arm = "X" if sequence == 0 else "Y"
            key = f"{case_id}:{role}:{arm}"
            mapping[key] = enabled
            tasks.append({"case_id": case_id, "fixture_hash": fixture["fixture_hash"],
                          "role": role, "anonymous_arm": arm, "sequence": sequence,
                          "provider": provider, "model": model,
                          "planned_provider_attempts": 1})
    return BlindPlan(evaluation_run_id, seed, tuple(tasks), mapping)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_flatten_strings(child))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for child in value:
            result.extend(_flatten_strings(child))
        return result
    return []


def _selected_case_literals(role: str, selected_case_ids: list[str]) -> list[str]:
    retriever = FewShotRetriever(enabled=True)
    if retriever.library is None:
        return []
    literals: list[str] = []
    cases = retriever.library.cases.get(role, ())
    for case in cases:
        if case.case_id not in selected_case_ids:
            continue
        literals.extend([case.case_id, case.title])
        literals.extend(_flatten_strings(case.facts))
    return sorted({item for item in literals if item and item.lower() not in {"unavailable", "available"}},
                  key=lambda item: (-len(item), item))


def _missing_evidence_acknowledged(role: str, report: dict[str, Any], facts: dict[str, Any]) -> bool:
    missing = [key for key, value in facts.items() if value == "unavailable"]
    if not missing:
        return True
    fields = ("missing_evidence", "missing_data", "data_gaps", "missing_information",
              "missing_sentiment_data", "data_quality_risks")
    return any(report.get(field) for field in fields if isinstance(report.get(field), list))


def _unavailable_to_confirmed(report: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    return sorted({hit["field"] for hit in unavailable_evidence_hits(report, facts)})


def evaluate_response(role: str, report: dict[str, Any], facts: dict[str, Any],
                      selected_case_ids: list[str] | None = None) -> dict[str, Any]:
    """Run deterministic hard checks only; no model judge is involved."""
    schema = ROLE_SCHEMAS_WITH_CHIEF[role]
    schema_errors: list[str] = []
    try:
        schema.model_validate(report)
    except (ValidationError, TypeError) as exc:
        schema_errors.append(type(exc).__name__)
    report_text = "\n".join(_flatten_strings(report))
    current_text = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    leak_hits = [literal for literal in _selected_case_literals(role, selected_case_ids or [])
                 if len(literal) >= 3 and literal not in current_text and literal in report_text]
    hallucination_hits = [str(item) for item in report.get("asserted_external_facts", [])]
    cap = confidence_cap(facts)
    confidence = report.get("confidence")
    declared_cap = report.get("confidence_cap", report.get("max_confidence_allowed"))
    confidence_hits = []
    if isinstance(confidence, (int, float)) and confidence > cap:
        confidence_hits.append(f"program_cap:{cap}")
    if isinstance(declared_cap, (int, float)) and declared_cap > cap:
        confidence_hits.append(f"declared_cap:{declared_cap}>{cap}")
    short_mid_present = role not in ("technical_analyst", "chief_researcher") or (
        "short_term_view" in report and "mid_term_view" in report)
    breakout_value = report.get("breakout_status", report.get("breakout_state"))
    trend_consistent = not (
        facts.get("breakout_status") in ("unconfirmed", "no_confirmed_breakout") and
        isinstance(breakout_value, str) and breakout_value.lower() in ("confirmed", "breakout_confirmed")
    )
    if role in ("technical_analyst", "risk_officer", "chief_researcher"):
        trend = report.get("trend_state")
        if facts.get("last_price") not in (None, "unavailable") and facts.get("ma20") not in (None, "unavailable"):
            if facts["last_price"] > facts["ma20"] and trend in ("strong_down", "downtrend"):
                trend_consistent = False
    invalidation_present = role not in ("technical_analyst", "risk_officer", "chief_researcher") or bool(
        report.get("invalidation_conditions") or report.get("protective_conditions"))
    forbidden_hits = trade_instruction_hits(report)
    semantic = (semantic_relation_violations(report, facts)
                if role in ALL_ROLES else {})
    return {
        "schema_compliance": not schema_errors,
        "schema_errors": schema_errors,
        "hallucination_rule_hits": hallucination_hits,
        "unavailable_to_confirmed": _unavailable_to_confirmed(report, facts),
        "confidence_cap": len(confidence_hits) == 0,
        "confidence_cap_hits": confidence_hits,
        "case_fact_leak": len(leak_hits) == 0,
        "case_fact_leak_hits": leak_hits,
        "missing_evidence_acknowledged": _missing_evidence_acknowledged(role, report, facts),
        "short_mid_fields_present": short_mid_present,
        "trend_consistency": trend_consistent,
        "invalidation_present": invalidation_present,
        "forbidden_trade_instruction": forbidden_hits,
        **semantic,
    }


class RoleIsolatedRunner:
    """Run exactly one mapped role per request, with no fallback or repair."""

    def __init__(self, *, registry: ModelRegistry | None = None,
                 role_config: dict[str, Any] | None = None,
                 tracker: UsageTracker | None = None,
                 client_factory: Callable[[ProviderConfig], Any] | None = None):
        bootstrap_eval_environment()
        self.registry = registry or ModelRegistry()
        self.role_config = role_config or load_role_config()
        self.tracker = tracker or UsageTracker(ROOT / "logs" / "p193_role_ab_usage.jsonl")
        self.router = LLMRouter(registry=self.registry, role_config=self.role_config,
                                tracker=self.tracker, client_factory=client_factory)
        self.prompt_agent = StockResearchAgent(
            router=self.router, prompt_dir=ROOT / "prompts",
            few_shot_retriever=FewShotRetriever(enabled=False),
        )

    @staticmethod
    def _schema(role: str) -> type[BaseModel]:
        return ROLE_SCHEMAS_WITH_CHIEF[role]

    def _candidate(self, role: str) -> tuple[dict[str, Any], ProviderConfig]:
        candidate = self.router._candidates(role)[0]
        provider = self.registry.for_model(candidate["provider"], candidate.get("model"))
        return candidate, provider

    @staticmethod
    def _eval_provider(role: str, provider: ProviderConfig) -> ProviderConfig:
        """Clone only the evaluation request settings; leave the registry untouched."""
        contract = EVAL_NATIVE_CONTRACTS.get(role)
        if contract is None:
            return provider
        expected_provider, expected_model, timeout, output_mode = contract
        if (provider.provider_name, provider.model_name) != (expected_provider, expected_model):
            raise ValueError(f"eval_native_contract_model_mismatch:{role}")
        non_thinking = role == "technical_analyst"
        return replace(provider, timeout=timeout, supports_reasoning_effort=True,
                       reasoning_levels=("none",) if non_thinking else ("low",),
                       reasoning_mode=None if non_thinking else provider.reasoning_mode,
                       supports_json_schema=output_mode == "json_schema")

    @staticmethod
    def _eval_response_format(role: str, schema: type[BaseModel]) -> dict[str, Any] | None:
        contract = EVAL_NATIVE_CONTRACTS.get(role)
        if contract is None:
            return None
        if contract[3] == "json_schema":
            # Pydantic remains the sole schema source. Extra fields are already forbidden.
            return {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": schema.model_json_schema()}}
        return {"type": "json_object"}

    def provider_readiness(self) -> dict[str, dict[str, Any]]:
        """Inspect configuration and construct adapters without requests or secrets."""
        expected = {
            "deepseek": ("technical_analyst", "chief_researcher"),
            "qwen": ("fundamental_event_analyst",),
            "doubao": ("sentiment_analyst",),
            "kimi": ("risk_officer",),
        }
        results: dict[str, dict[str, str]] = {}
        for provider_name, roles in expected.items():
            key_status = "missing"
            model_status = "invalid"
            endpoint_status = "invalid"
            mapping_status = "invalid"
            adapter_status = "error"
            prompt_status = "error"
            schema_status = "error"
            try:
                candidates = [self._candidate(role)[1] for role in roles]
                provider = candidates[0]
                key_status = "configured" if provider.api_key else "missing"
                model_status = _config_status(provider.model_name)
                endpoint_status = _endpoint_status(provider.base_url)
                mapping_status = "configured" if all(
                    candidate.provider_name == provider_name and
                    candidate.model_name == provider.model_name for candidate in candidates
                ) else "invalid"
                ADAPTERS.get(provider_name, OpenAICompatibleAdapter)(provider)
                adapter_status = "ready"
                for role in roles:
                    if not self.prompt_agent._prompt(role):
                        raise ValueError("prompt_empty")
                    if not self._schema(role).model_json_schema():
                        raise ValueError("schema_empty")
                prompt_status = "ready"
                schema_status = "ready"
            except (KeyError, ValueError, TypeError, OSError):
                pass
            ready = (key_status == "configured" and model_status == "configured" and
                     endpoint_status == "configured" and mapping_status == "configured" and
                     adapter_status == prompt_status == schema_status == "ready")
            cap_status = output_limit_status(provider) if adapter_status == "ready" else {"status": "UNSUPPORTED", "parameter": None, "reasoning_accounting": "unknown"}
            results[provider_name] = {
                "key": key_status, "model": model_status, "endpoint": endpoint_status,
                "mapping": mapping_status, "adapter": adapter_status,
                "prompt": prompt_status, "schema": schema_status,
                "output_limit": cap_status,
                "readiness": ("UNSUPPORTED" if ready and cap_status["status"] != "VERIFIED" else
                              "VERIFIED" if ready else "BLOCKED_CONFIG"),
            }
        return results

    def prepare_request(self, role: str, fixture: dict[str, Any], few_shot_enabled: bool,
                        evaluation_run_id: str, synthetic_bundle: dict[str, Any] | None = None) -> dict[str, Any]:
        if role not in ALL_ROLES:
            raise ValueError(f"unknown_eval_role:{role}")
        facts = fixture["facts"]
        retriever = FewShotRetriever(enabled=few_shot_enabled)
        derived_relations: dict[str, Any] | None = None
        if role == "chief_researcher":
            bundle = synthetic_bundle or load_synthetic_specialist_bundle()
            if bundle["case_id"] != fixture["case_id"]:
                raise ValueError("chief_bundle_case_mismatch")
            specialist_reports = bundle["specialists"]
            specialist_context = {name: {"success": True, "data": data}
                                  for name, data in specialist_reports.items()}
            few_text, audit = retriever.retrieve(role, facts, specialist_context)
            payload = {"analysis_id": evaluation_run_id, "FACT DATA": facts,
                       "specialist_success_count": len(specialist_reports), "status": "complete",
                       "missing_roles": [], "successful_role_reports": specialist_reports}
            bundle_hash = bundle["bundle_hash"]
        else:
            few_text, audit = retriever.retrieve(role, facts)
            role_facts = StockResearchAgent._role_facts(role, facts)
            if role in ("technical_analyst", "risk_officer"):
                derived_relations = derive_fact_relations(facts)
                role_facts = {**role_facts,
                              "derived_fact_relations": {
                                  key: value for key, value in derived_relations.items()
                                  if key not in ("source_context", "allowed_source_labels")},
                              "source_context": derived_relations["source_context"],
                              "allowed_source_labels": derived_relations["allowed_source_labels"]}
            payload = "FACT DATA（只读）：" + json.dumps(
                role_facts, ensure_ascii=False, default=str)
            bundle_hash = None
        system_prompt = self.prompt_agent._prompt(role, few_text)
        if role in ("technical_analyst", "risk_officer"):
            system_prompt += "\n\n" + FACT_RELATION_DISCIPLINE + "\n\n" + FACT_SEMANTIC_DISCIPLINE
        messages = [{"role": "system", "content": system_prompt +
                     "\n\n" + STRUCTURED_OUTPUT_DISCIPLINE},
                    {"role": "user", "content": payload if isinstance(payload, str) else
                     json.dumps(payload, ensure_ascii=False, default=str)}]
        schema = self._schema(role)
        constrained = self.router._schema_messages(messages, schema)
        candidate, provider = self._candidate(role)
        provider = self._eval_provider(role, provider)
        return {"role": role, "facts": facts, "messages": constrained, "schema": schema,
                "audit": audit, "candidate": candidate, "provider": provider,
                "reasoning_effort": ("none" if role == "technical_analyst" else "low")
                                    if role in EVAL_NATIVE_CONTRACTS else
                                    (candidate.get("reasoning") or {}).get("standard"),
                "response_format": self._eval_response_format(role, schema),
                "schema_hash": canonical_hash(schema.model_json_schema()),
                "fact_bundle_hash": stable_fact_bundle_hash(facts),
                "chief_bundle_hash": bundle_hash,
                "prompt_hash": canonical_hash(constrained),
                "derived_fact_relations": derived_relations,
                "few_shot_enabled": few_shot_enabled,
                "max_output_tokens": OUTPUT_CAPS[role], "output_limit": output_limit_status(provider),
                "few_shot_token_estimate": audit.get("estimated_few_shot_tokens", 0),
                "selected_case_ids": list(audit.get("selected_case_ids", [])),
                "retrieval_scores": list(audit.get("retrieval_scores", []))}

    def run_role(self, role: str, fixture: dict[str, Any], *, few_shot_enabled: bool,
                 evaluation_run_id: str, anonymous_arm: str, synthetic_bundle: dict[str, Any] | None = None,
                 dry_run: bool = True, allow_network: bool = False,
                 baseline_gate_passed: bool = False) -> dict[str, Any]:
        if not dry_run and not allow_network:
            raise PermissionError("live_eval_requires_explicit_allow_network")
        if not dry_run and few_shot_enabled and not baseline_gate_passed:
            raise PermissionError("baseline_reliability_gate_not_passed")
        prepared = self.prepare_request(role, fixture, few_shot_enabled, evaluation_run_id, synthetic_bundle)
        provider = prepared["provider"]
        base = {
            "evaluation_run_id": evaluation_run_id, "case_id": fixture["case_id"],
            "fixture_hash": fixture["fixture_hash"], "role": role,
            "few_shot_enabled": few_shot_enabled,
            "anonymous_arm": anonymous_arm, "provider": provider.provider_name,
            "model": provider.model_name, "selected_case_ids": prepared["selected_case_ids"],
            "reasoning_effort": prepared["reasoning_effort"],
            "response_format_type": (prepared["response_format"] or {}).get("type"),
            "schema_hash": prepared["schema_hash"],
            "retrieval_scores": prepared["retrieval_scores"],
            "few_shot_token_estimate": prepared["few_shot_token_estimate"],
            "prompt_hash": prepared["prompt_hash"], "fact_bundle_hash": prepared["fact_bundle_hash"],
            "chief_bundle_hash": prepared["chief_bundle_hash"],
            "derived_fact_relations": prepared["derived_fact_relations"],
            "fallback_used": False, "repair_used": False, "provider_attempts": 0,
            "schema_valid": None, "status": "PLANNED", "input_tokens": None,
            "output_tokens": None, "total_tokens": None, "latency": None,
            "estimated_cost": None, "cost_status": "unknown", "parsed_response": None,
            "raw_response": None, "metrics": None, "finish_reason": None,
        }
        base.update(usage_accounting(None))
        base.update({"runtime_diagnostics": diagnostics(provider, role, OUTPUT_CAPS[role]),
                     "max_output_tokens": OUTPUT_CAPS[role], "output_limit": prepared["output_limit"]})
        if dry_run:
            return base
        if not provider.configured:
            return {**base, "status": "PROVIDER_ERROR", "error": "provider_not_configured"}
        if prepared["output_limit"]["status"] != "VERIFIED":
            return {**base, "status": "UNSUPPORTED", "error": "provider_output_limit_unsupported"}
        started = perf_counter()
        diag = base["runtime_diagnostics"]
        diag["request_started_at"] = now()
        base["provider_attempts"] = 1
        result = dict(base)
        try:
            response = self.router._request(provider, prepared["messages"],
                                            prepared["reasoning_effort"], prepared["schema"],
                                            max_output_tokens=OUTPUT_CAPS[role])
            diag["response_received"] = True
            diag["request_finished_at"] = now()
            diag["elapsed_seconds"] = perf_counter() - started
            result.update(usage_accounting(response))
            finish = getattr(response.choices[0], "finish_reason", None) if getattr(response, "choices", None) else None
            result["finish_reason"] = finish if finish in ("stop", "length", "content_filter", "tool_calls", "function_call") else None
            raw = self.router._content(response)
            result["raw_response"] = raw
            if finish == "length":
                result.update(status="SCHEMA_FAIL", schema_valid=False, error="output_cap_hit")
            else:
                parsed = prepared["schema"].model_validate_json(raw)
                metrics = evaluate_response(role, parsed.model_dump(), fixture["facts"], prepared["selected_case_ids"])
                semantic_failed = any(metrics.get(name) is True for name in (
                    "numeric_relation_violation", "temporal_semantics_violation", "provenance_violation",
                    "semantic_transformation_violation", "unsupported_trajectory_claim", "trade_instruction_violation"))
                semantic_failed = semantic_failed or bool(metrics["unavailable_to_confirmed"])
                result.update(status="SEMANTIC_FAIL" if semantic_failed else
                              "PASS" if metrics["schema_compliance"] else "SCHEMA_FAIL",
                              schema_valid=metrics["schema_compliance"], parsed_response=parsed.model_dump(), metrics=metrics)
        except Exception as exc:
            diag["exception_type"] = type(exc).__name__
            diag["timeout_phase"] = timeout_phase(exc)
            result.update(status="SCHEMA_FAIL" if isinstance(exc, ValidationError) else "PROVIDER_ERROR",
                          schema_valid=False, error=type(exc).__name__)
        finally:
            if diag["request_finished_at"] is None:
                diag["request_finished_at"] = now()
                diag["elapsed_seconds"] = perf_counter() - started
        result["latency"] = diag["elapsed_seconds"]
        if result["input_tokens"] is not None and result["output_tokens"] is not None:
            result["estimated_cost"], result["cost_status"] = self.router._cost(
                provider, result["input_tokens"], result["output_tokens"])
        self.tracker.record(UsageRecord(
            evaluation_run_id, provider.provider_name, provider.model_name, role,
            result["input_tokens"], result["output_tokens"], result["total_tokens"],
            result["latency"], result["estimated_cost"], timestamp_now(), result["status"] == "PASS", False,
            error=result.get("error"), analysis_mode="standard", requested_model=provider.model_name,
            actual_model=provider.model_name, usage_status=result["usage_accounting_status"],
            schema_repair_count=0, cost_status=result["cost_status"], timeout_stage=diag["timeout_phase"]))
        return result

    def build_plan(self, evaluation_run_id: str) -> BlindPlan:
        return build_blind_plan(evaluation_run_id, self.role_config, self.registry)

    def build_baseline_smoke_plan(self, evaluation_run_id: str = "p193e-baseline-smoke") -> dict[str, Any]:
        """Plan three OFF-only requests. This method never calls a provider."""
        tasks: list[dict[str, Any]] = []
        for case_id, role in BASELINE_SMOKE_PAIRS:
            fixture = load_fixture(case_id)
            prepared = self.prepare_request(role, fixture, False, evaluation_run_id)
            contract = prepared["output_limit"]
            if contract["status"] != "VERIFIED":
                raise ValueError(f"baseline_output_contract_unverified:{role}")
            provider = prepared["provider"]
            tasks.append({"case_id": case_id, "fixture_hash": fixture["fixture_hash"],
                          "fact_bundle_hash": prepared["fact_bundle_hash"], "role": role,
                          "schema_hash": prepared["schema_hash"],
                          "few_shot_enabled": False, "provider": provider.provider_name,
                          "model": provider.model_name, "reasoning_effort": prepared["reasoning_effort"],
                          "response_format_type": prepared["response_format"]["type"],
                          "timeout_seconds": provider.timeout,
                          "output_cap_field": contract["parameter"],
                          "output_cap": prepared["max_output_tokens"],
                          "planned_provider_attempts": 1})
        return {"evaluation_run_id": evaluation_run_id, "gate": "BASELINE_RELIABILITY_GATE",
                "gate_status": "NOT_RUN", "planned_calls": len(tasks),
                "max_provider_attempts": len(tasks), "few_shot_off_calls": len(tasks),
                "few_shot_on_calls": 0,
                "provider_counts": dict(Counter(task["provider"] for task in tasks)),
                "fallback_calls": 0, "repair_calls": 0, "retry_calls": 0,
                "judge_model_calls": 0, "tasks": tasks}

    def build_baseline_recheck_plan(self, evaluation_run_id: str = "p193g-baseline-recheck") -> dict[str, Any]:
        """Plan only the two failed OFF roles; reuse Qwen solely after compatibility review."""
        tasks: list[dict[str, Any]] = []
        for case_id, role in BASELINE_RECHECK_PAIRS:
            fixture = load_fixture(case_id)
            prepared = self.prepare_request(role, fixture, False, evaluation_run_id)
            contract = prepared["output_limit"]
            if contract["status"] != "VERIFIED":
                raise ValueError(f"baseline_recheck_output_contract_unverified:{role}")
            provider = prepared["provider"]
            tasks.append({"case_id": case_id, "fixture_hash": fixture["fixture_hash"],
                          "fact_bundle_hash": prepared["fact_bundle_hash"], "role": role,
                          "schema_hash": prepared["schema_hash"], "prompt_hash": prepared["prompt_hash"],
                          "few_shot_enabled": False, "selected_case_ids": [],
                          "provider": provider.provider_name, "model": provider.model_name,
                          "reasoning_effort": prepared["reasoning_effort"],
                          "response_format_type": prepared["response_format"]["type"],
                          "timeout_seconds": provider.timeout,
                          "output_cap_field": contract["parameter"],
                          "output_cap": prepared["max_output_tokens"],
                          "planned_provider_attempts": 1})
        return {"evaluation_run_id": evaluation_run_id, "gate": "BASELINE_RELIABILITY_GATE",
                "gate_status": "NOT_RUN", "planned_calls": len(tasks),
                "max_provider_attempts": len(tasks), "few_shot_off_calls": len(tasks),
                "few_shot_on_calls": 0,
                "provider_counts": {"deepseek": 1, "kimi": 1, "qwen": 0, "doubao": 0},
                "fallback_calls": 0, "repair_calls": 0, "retry_calls": 0,
                "judge_model_calls": 0, "reused_baseline_role": "fundamental_event_analyst",
                "tasks": tasks}

    def qwen_baseline_reuse_check(self, prior_result: dict[str, Any]) -> dict[str, Any]:
        """Compare the previous PASS with today's unchanged Qwen request contract."""
        fixture = load_fixture("GC04")
        prepared = self.prepare_request("fundamental_event_analyst", fixture, False,
                                        prior_result.get("evaluation_run_id", "p193e-baseline"))
        provider = prepared["provider"]
        expected = {
            "case_id": fixture["case_id"], "fixture_hash": fixture["fixture_hash"],
            "fact_bundle_hash": prepared["fact_bundle_hash"],
            "role": "fundamental_event_analyst", "provider": provider.provider_name,
            "model": provider.model_name, "prompt_hash": prepared["prompt_hash"],
            "schema_hash": prepared["schema_hash"], "few_shot_enabled": False,
            "selected_case_ids": [], "reasoning_effort": "low",
            "response_format_type": "json_schema", "max_output_tokens": 3072,
            "provider_attempts": 1, "fallback_used": False, "repair_used": False,
            "status": "PASS", "schema_valid": True, "finish_reason": "stop",
        }
        reasons = [key for key, value in expected.items() if prior_result.get(key) != value]
        runtime = prior_result.get("runtime_diagnostics") or {}
        if runtime.get("response_received") is not True or (runtime.get("timeout_config") or {}).get("read") != 90.0:
            reasons.append("runtime_contract")
        metrics = prior_result.get("metrics") or {}
        if metrics.get("confidence_cap") is not True or metrics.get("hallucination_rule_hits"):
            reasons.append("prior_fact_checks")
        if prepared["output_limit"]["status"] != "VERIFIED" or provider.timeout != 90.0:
            reasons.append("current_provider_contract")
        return {"reusable": not reasons, "mismatches": reasons,
                "prior_evaluation_run_id": prior_result.get("evaluation_run_id")}

    def combine_baseline_recheck_gate(self, prior_qwen: dict[str, Any],
                                      recheck_results: list[dict[str, Any]],
                                      manual_fact_review: dict[str, bool] | None = None) -> dict[str, Any]:
        """Require prior Qwen compatibility and two new reviewed OFF baselines."""
        reuse = self.qwen_baseline_reuse_check(prior_qwen)
        observed = [(result.get("case_id", "").split("_", 1)[0], result.get("role"))
                    for result in recheck_results]
        if not reuse["reusable"] or observed != list(BASELINE_RECHECK_PAIRS):
            return {"status": "FAIL", "failures": ["qwen_reuse_or_recheck_plan_mismatch"],
                    "qwen_reuse": reuse}
        gate = baseline_reliability_gate([recheck_results[0], prior_qwen, recheck_results[1]],
                                         manual_fact_review)
        return {**gate, "qwen_reuse": reuse}

    def run_baseline_smoke(self, evaluation_run_id: str = "p193e-baseline-smoke", *,
                           dry_run: bool = True, allow_network: bool = False,
                           manual_fact_review: dict[str, bool] | None = None) -> dict[str, Any]:
        """OFF-only role smoke; live execution requires a separate explicit opt-in."""
        if not dry_run and not allow_network:
            raise PermissionError("live_eval_requires_explicit_allow_network")
        plan = self.build_baseline_smoke_plan(evaluation_run_id)
        results = [self.run_role(task["role"], load_fixture(task["case_id"]),
                                 few_shot_enabled=False, evaluation_run_id=evaluation_run_id,
                                 anonymous_arm="OFF", dry_run=dry_run, allow_network=allow_network)
                   for task in plan["tasks"]]
        attempts = sum(result["provider_attempts"] for result in results)
        if attempts > plan["max_provider_attempts"]:
            raise AssertionError("baseline_max_provider_attempts_exceeded")
        return {"plan": plan, "actual_provider_attempts": attempts,
                "gate": baseline_reliability_gate(results, manual_fact_review) if not dry_run else
                        baseline_reliability_gate([]), "results": results}

    @staticmethod
    def _estimated_input_tokens(prepared: dict[str, Any]) -> int:
        return sum(estimate_tokens(message["content"]) for message in prepared["messages"])

    def estimate_round1_budget(self, evaluation_run_id: str = "p193a-round1-smoke") -> dict[str, Any]:
        """Estimate the six-call validation plan; unsupported caps block execution."""
        plan = self.build_plan(evaluation_run_id)
        bundle = load_synthetic_specialist_bundle()
        rows: list[dict[str, Any]] = []
        providers: dict[str, dict[str, Any]] = {}
        for task in plan.public_tasks:
            fixture = load_fixture(task["case_id"])
            role = task["role"]
            chief_bundle = bundle if role == "chief_researcher" else None
            off = self.prepare_request(role, fixture, False, evaluation_run_id, chief_bundle)
            on = self.prepare_request(role, fixture, True, evaluation_run_id, chief_bundle)
            off_tokens = self._estimated_input_tokens(off)
            on_tokens = self._estimated_input_tokens(on)
            if task["sequence"] == 0:
                rows.append({"case_id": task["case_id"], "role": role,
                             "provider": task["provider"], "model": task["model"],
                             "off_input_tokens": off_tokens, "on_input_tokens": on_tokens,
                             "few_shot_delta": on_tokens - off_tokens,
                             "output_tokens_per_arm": OUTPUT_CAPS[role],
                             "selected_case_ids": on["selected_case_ids"],
                             "output_limit": on["output_limit"]})
                item = providers.setdefault(task["provider"], {
                    "off_input_tokens": 0, "on_input_tokens": 0, "few_shot_delta": 0,
                    "output_tokens": 0, "calls": 0, "model": task["model"],
                })
                item["off_input_tokens"] += off_tokens
                item["on_input_tokens"] += on_tokens
                item["few_shot_delta"] += on_tokens - off_tokens
                item["output_tokens"] += OUTPUT_CAPS[role] * 2
                item["calls"] += 2
        for provider_name, item in providers.items():
            provider = self.registry.get(provider_name)
            if provider.cost_profile is None:
                item["cost_status"] = "unknown"
                item["estimated_cost"] = None
            else:
                rates = provider.cost_profile
                item["cost_status"] = "estimated"
                item["estimated_cost"] = round((item["off_input_tokens"] * rates["input_per_million"] +
                                                 item["on_input_tokens"] * rates["input_per_million"] +
                                                 item["output_tokens"] * rates["output_per_million"]) / 1_000_000, 8)
        cost_status = "estimated" if all(item["cost_status"] == "estimated" for item in providers.values()) else "unknown"
        total_cost = (round(sum(item["estimated_cost"] for item in providers.values()), 8)
                      if cost_status == "estimated" else None)
        return {
            "planned_base_calls": len(plan.public_tasks),
            "max_provider_attempts": len(plan.public_tasks),
            "provider_counts": dict(Counter(task["provider"] for task in plan.public_tasks)),
            "off_input_tokens": sum(row["off_input_tokens"] for row in rows),
            "on_input_tokens": sum(row["on_input_tokens"] for row in rows),
            "few_shot_extra_tokens": sum(row["few_shot_delta"] for row in rows),
            "output_tokens": sum(row["output_tokens_per_arm"] * 2 for row in rows),
            "cost_status": cost_status,
            "estimated_cost": total_cost,
            "providers": providers,
            "pairs": rows,
            "chief_bundle_hash": bundle["bundle_hash"],
            "fallback_enabled": False,
            "repair_enabled": False,
            "judge_model_calls": 0, "retry_calls": 0,
            "output_budget_status": "requested_caps_not_billed_token_bound",
        }

    def run_round1(self, evaluation_run_id: str = "p193a-round1-smoke", *, dry_run: bool = True,
                   allow_network: bool = False, save_evidence_to: Path | None = None,
                   baseline_results: list[dict[str, Any]] | None = None,
                   manual_fact_review: dict[str, bool] | None = None) -> dict[str, Any]:
        if not dry_run and baseline_reliability_gate(
                baseline_results or [], manual_fact_review)["status"] != "PASS":
            raise PermissionError("baseline_reliability_gate_not_passed")
        plan = self.build_plan(evaluation_run_id)
        bundle = load_synthetic_specialist_bundle()
        results = []
        for task in plan.public_tasks:
            enabled = plan.private_mapping[
                f"{task['case_id']}:{task['role']}:{task['anonymous_arm']}"
            ]
            fixture = load_fixture(task["case_id"])
            chief_bundle = bundle if task["role"] == "chief_researcher" else None
            results.append(self.run_role(
                task["role"], fixture, few_shot_enabled=enabled,
                evaluation_run_id=evaluation_run_id, anonymous_arm=task["anonymous_arm"],
                synthetic_bundle=chief_bundle, dry_run=dry_run, allow_network=allow_network,
                baseline_gate_passed=not dry_run))
        summary = {"planned_base_calls": len(plan.public_tasks),
                   "max_provider_attempts": sum(item["planned_provider_attempts"] for item in plan.public_tasks),
                   "actual_provider_calls": sum(item["provider_attempts"] for item in results),
                   "provider_counts": dict(Counter(item["provider"] for item in plan.public_tasks)),
                   "fallback_calls": 0, "repair_calls": 0, "judge_model_calls": 0, "retry_calls": 0,
                   "results": [public_evidence_record(result) for result in results]}
        if save_evidence_to is not None and not dry_run:
            save_evidence(save_evidence_to, plan, results)
        return {"plan": plan.public_dict(), "summary": summary}


def public_evidence_record(result: dict[str, Any]) -> dict[str, Any]:
    """Strip the private OFF/ON bit before blind evidence is persisted."""
    return {key: value for key, value in result.items() if key != "few_shot_enabled"}


def save_evidence(directory: Path, plan: BlindPlan, results: list[dict[str, Any]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    public = [public_evidence_record(result) for result in results]
    (directory / "public_blind_results.json").write_text(
        json.dumps({"plan": plan.public_dict(), "results": public}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (directory / "blind_mapping.json").write_text(
        json.dumps({"evaluation_run_id": plan.evaluation_run_id, "seed": plan.seed,
                    "mapping": plan.private_mapping}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline role-isolated baseline recheck planning")
    parser.add_argument("--preflight", action="store_true", help="print sanitized local readiness only")
    args = parser.parse_args()
    runner = RoleIsolatedRunner()
    if args.preflight:
        print(json.dumps(runner.provider_readiness(), ensure_ascii=False, indent=2))
        return
    print(json.dumps(runner.build_baseline_recheck_plan(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
