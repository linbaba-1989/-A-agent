from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


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
