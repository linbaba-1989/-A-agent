from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str
    symbol: str
    timestamp: str
    fact_bundle_hash: str
    role_outputs: dict[str, Any]
    chief_output: dict[str, Any]
    forward_return_1d: float | None = None
    forward_return_3d: float | None = None
    forward_return_5d: float | None = None
    outcome_status: Literal["pending", "partial", "complete"] = "pending"
    arena_evidence: list[dict[str, Any]] = Field(default_factory=list)


class EmployeeScorecard(BaseModel):
    """Versioned structure for future role/model performance history."""
    model_config = ConfigDict(extra="forbid")
    role: str
    provider: str
    model_id: str
    model_version: str | None = None
    sample_count: int = 0
    schema_pass_rate: float | None = None
    hallucination_rate: float | None = None
    role_quality_score: float | None = None
    timeout_rate: float | None = None
    average_latency: float | None = None
    average_cost: float | None = None
    cost_status: Literal["estimated", "unknown"] = "unknown"
