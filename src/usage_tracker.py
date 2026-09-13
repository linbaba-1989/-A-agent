"""Thread-safe JSONL model usage accounting without prompts, facts, or secrets."""
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any


def timestamp_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class UsageRecord:
    analysis_id: str
    provider: str
    model: str
    role: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency: float
    estimated_cost: float | None
    timestamp: str
    success: bool
    fallback: bool = False
    error: str | None = None
    analysis_mode: str = "standard"
    reasoning_effort: str | None = None
    requested_model: str | None = None
    actual_model: str | None = None
    fallback_reason: str | None = None
    usage_status: str = "available"
    schema_repair_count: int = 0
    connect_latency: float | None = None
    first_token_latency: float | None = None
    timeout_stage: str | None = None
    cost_status: str = "unknown"


class UsageTracker:
    def __init__(self, path: str | Path = "logs/model_usage.jsonl"):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, record: UsageRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock, self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def summary(self) -> dict[str, dict[str, float]]:
        totals: dict[str, dict[str, float]] = {}
        for row in self.records():
            key = row["role"]
            item = totals.setdefault(key, {"calls": 0, "total_tokens": 0, "estimated_cost": 0.0,
                                           "cost_status": "estimated"})
            item["calls"] += 1
            item["total_tokens"] += row["total_tokens"] or 0
            if row.get("estimated_cost") is None:
                item["estimated_cost"] = None
                item["cost_status"] = "unknown"
            elif item["estimated_cost"] is not None:
                item["estimated_cost"] += row["estimated_cost"]
        return totals
