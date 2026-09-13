"""Offline-first, role-specific Model Arena scoring and aggregation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from statistics import median
from typing import Any


ROLES = ("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
         "risk_officer", "chief_researcher")
HARD_FACT_CATEGORIES = ("公告", "订单", "中标", "机构资金", "主力", "北向资金", "龙虎榜",
                        "融资融券", "市场涨停数量", "板块排名", "龙头地位")


def stable_fact_bundle_hash(fact_bundle: dict[str, Any]) -> str:
    payload = json.dumps(fact_bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArenaScore:
    provider: str
    model_id: str
    role: str
    fact_bundle_hash: str
    schema_pass: bool
    fact_grounding: bool
    hallucination_count: int
    a_share_logic_score: int
    sentiment_logic_score: int
    risk_specificity_score: int
    evidence_quality_score: int
    data_gap_awareness_score: int
    chief_conflict_handling_score: int
    confidence_discipline_score: int
    score_reason: str
    hard_fail: bool
    hard_fail_reasons: tuple[str, ...]
    latency: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    cost_status: str = "unknown"
    fallback_count: int = 0
    schema_repair_count: int = 0
    timeout: bool = False

    @property
    def overall_score(self) -> float:
        if self.hard_fail:
            return 0.0
        values = (self.a_share_logic_score, self.sentiment_logic_score, self.risk_specificity_score,
                  self.evidence_quality_score, self.data_gap_awareness_score,
                  self.chief_conflict_handling_score, self.confidence_discipline_score)
        return round(sum(values) / len(values), 2)

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "overall_score": self.overall_score,
                "status": "Hard Fail" if self.hard_fail else "PASS"}


def score_result(*, provider: str, model_id: str, role: str, fact_bundle_hash: str,
                 report: dict[str, Any], metrics: dict[str, Any] | None = None,
                 confidence_cap: int = 100) -> ArenaScore:
    metrics = metrics or {}
    asserted = report.get("asserted_external_facts", [])
    hallucinations = [str(item) for item in asserted]
    declared_hallucinations = int(report.get("hallucination_count", 0) or 0)
    hard_reasons = []
    if hallucinations or declared_hallucinations:
        hard_reasons.append("unsupported_external_fact")
    if not isinstance(report, dict) or report.get("schema_pass") is False:
        hard_reasons.append("schema_failure")
    confidence = report.get("confidence")
    if isinstance(confidence, (int, float)) and confidence > confidence_cap:
        hard_reasons.append("confidence_cap_violation")
    if report.get("confirmed_unavailable"):
        hard_reasons.append("unavailable_as_confirmed")

    def score(name: str, default: int = 3) -> int:
        value = report.get(name, default)
        return max(0, min(5, int(value)))

    schema_pass = "schema_failure" not in hard_reasons
    fact_grounding = not hallucinations and declared_hallucinations == 0 and "unavailable_as_confirmed" not in hard_reasons
    reason = report.get("score_reason") or (
        "硬性事实或结构规则未通过" if hard_reasons else "结构、事实边界与角色逻辑达到合格线")
    return ArenaScore(
        provider, model_id, role, fact_bundle_hash, schema_pass, fact_grounding,
        len(hallucinations) + declared_hallucinations,
        score("a_share_logic_score"), score("sentiment_logic_score"), score("risk_specificity_score"),
        score("evidence_quality_score"), score("data_gap_awareness_score"),
        score("chief_conflict_handling_score", 3 if role == "chief_researcher" else 5),
        0 if "confidence_cap_violation" in hard_reasons else score("confidence_discipline_score", 5),
        reason, bool(hard_reasons), tuple(hard_reasons), metrics.get("latency"),
        metrics.get("input_tokens"), metrics.get("output_tokens"), metrics.get("total_tokens"),
        metrics.get("estimated_cost"), metrics.get("cost_status", "unknown"),
        int(metrics.get("fallback_count", 0)), int(metrics.get("schema_repair_count", 0)),
        bool(metrics.get("timeout", False)))


def role_leaderboard(rows: list[ArenaScore], role: str) -> list[dict[str, Any]]:
    if role not in ROLES:
        raise ValueError(f"unknown_arena_role: {role}")
    selected = [row for row in rows if row.role == role]
    selected.sort(key=lambda row: (row.hard_fail, -row.overall_score,
                                   row.latency if row.latency is not None else float("inf"), row.model_id))
    return [row.public_dict() for row in selected]


def reliability(rows: list[ArenaScore]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "success_rate": None, "schema_pass_rate": None, "timeout_rate": None,
                "repair_rate": None, "fallback_rate": None, "median_latency": None, "p95_latency": None}
    count = len(rows)
    latencies = [row.latency for row in rows if row.latency is not None]
    return {
        "samples": count,
        "success_rate": sum(not row.hard_fail for row in rows) / count,
        "schema_pass_rate": sum(row.schema_pass for row in rows) / count,
        "timeout_rate": sum(row.timeout for row in rows) / count,
        "repair_rate": sum(row.schema_repair_count > 0 for row in rows) / count,
        "fallback_rate": sum(row.fallback_count > 0 for row in rows) / count,
        "median_latency": median(latencies) if latencies else None,
        "p95_latency": None if len(latencies) < 2 else sorted(latencies)[int(0.95 * (len(latencies) - 1))],
    }
