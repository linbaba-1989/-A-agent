from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORBIDDEN_CLAIMS = ("主力吸筹", "主力洗盘", "庄家控盘", "机构抢筹", "必涨", "强烈买入")
VAGUE_RISK = ("注意市场风险", "投资有风险", "谨慎投资")
PHASE_WORDS = ("冰点", "修复", "回暖", "加速", "高潮", "分歧", "退潮")


def hallucination_claims(report: Any) -> list[str]:
    text = json.dumps(report, ensure_ascii=False, default=str)
    return [claim for claim in FORBIDDEN_CLAIMS if claim in text]


def sentiment_phase_allowed(factors: dict[str, Any]) -> bool:
    return factors.get("market_phase", {}).get("status") == "available"


def evaluate_output(report: dict[str, Any], available_facts: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(report, ensure_ascii=False, default=str)
    hallucinations = hallucination_claims(report)
    unsupported = [str(value) for value in report.get("asserted_external_facts", [])
                   if value not in available_facts.values()]
    return {"schema_pass": isinstance(report, dict), "fact_grounding": not unsupported,
            "hallucination_count": len(hallucinations) + len(unsupported),
            "a_share_logic_score": 1 if not hallucinations else 0,
            "sentiment_logic_score": 0 if any(word in text for word in PHASE_WORDS)
            and report.get("market_phase_status") == "unavailable" else 1,
            "risk_specificity": 0 if any(item in text for item in VAGUE_RISK) else 1,
            "evidence_quality": 1 if report.get("evidence") else 0,
            "data_gap_awareness": 1 if report.get("data_gaps") else 0,
            "latency": report.get("latency"), "tokens": report.get("tokens"), "cost": report.get("cost")}


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or Path(__file__).resolve().parents[1] / "evals" / "a_share" / "scenarios.jsonl"
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_mock_eval() -> dict[str, Any]:
    rows = []
    for scenario in load_scenarios():
        report = {"evidence": [scenario["scenario"]], "data_gaps": ["未提供数据"],
                  "market_phase_status": "unavailable", "summary": "仅依据输入证据，缺失项不推断。"}
        rows.append({"id": scenario["id"], **evaluate_output(report, scenario["facts"])})
    return {"scenario_count": len(rows), "passed": sum(row["hallucination_count"] == 0 for row in rows),
            "rows": rows}
