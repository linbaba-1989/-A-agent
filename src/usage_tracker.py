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
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency: float
    estimated_cost: float
    timestamp: str
    success: bool
    fallback: bool = False
    error: str | None = None


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
            item = totals.setdefault(key, {"calls": 0, "total_tokens": 0, "estimated_cost": 0.0})
            item["calls"] += 1
            item["total_tokens"] += row["total_tokens"]
            item["estimated_cost"] += row["estimated_cost"]
        return totals
