"""Parallel AI workforce. Models interpret immutable QMT facts only."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .agent_schemas import (ChiefReport, FundamentalEventReport, RiskReport,
                            SentimentReport, TechnicalReport)
from .llm_router import LLMRouter, RouterResult

ROLE_SCHEMAS = {
    "technical_analyst": TechnicalReport,
    "fundamental_event_analyst": FundamentalEventReport,
    "sentiment_analyst": SentimentReport,
    "risk_officer": RiskReport,
}
PROMPT_FILES = {
    "technical_analyst": "technical.md",
    "fundamental_event_analyst": "fundamental.md",
    "sentiment_analyst": "sentiment.md",
    "risk_officer": "risk.md",
    "chief_researcher": "chief.md",
}


class StockResearchAgent:
    def __init__(self, router: LLMRouter | None = None, prompt_dir: str | Path | None = None):
        self.router = router or LLMRouter()
        self.prompt_dir = Path(prompt_dir or Path(__file__).resolve().parents[1] / "prompts")

    def _prompt(self, role: str) -> str:
        return (self.prompt_dir / PROMPT_FILES[role]).read_text(encoding="utf-8")

    @staticmethod
    def analysis_id(symbol: str, now: datetime | None = None) -> str:
        return f"{symbol}_{(now or datetime.now()).strftime('%Y%m%d_%H%M%S')}"

    def _role_call(self, role: str, facts: dict[str, Any], analysis_id: str, analysis_mode: str) -> RouterResult:
        messages = [{"role": "system", "content": self._prompt(role)},
                    {"role": "user", "content": "FACT DATA（只读）：\n" + json.dumps(facts, ensure_ascii=False, default=str)}]
        return self.router.call(role, messages, ROLE_SCHEMAS[role], analysis_id, analysis_mode)

    def analyze(self, symbol: str, fact_data: dict[str, Any], analysis_mode: str = "standard") -> dict[str, Any]:
        analysis_id = self.analysis_id(symbol)
        immutable_facts = json.loads(json.dumps(fact_data, ensure_ascii=False, default=str))
        role_results: dict[str, RouterResult] = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai-employee") as pool:
            futures = {pool.submit(self._role_call, role, immutable_facts, analysis_id, analysis_mode): role
                       for role in ROLE_SCHEMAS}
            for future in as_completed(futures):
                role = futures[future]
                try:
                    role_results[role] = future.result()
                except Exception as exc:
                    role_results[role] = RouterResult(role, None, None, False, None, 0.0,
                                                      error=f"{type(exc).__name__}: {exc}")
        chief_input = {role: result.data if result.success else "unavailable" for role, result in role_results.items()}
        chief_messages = [{"role": "system", "content": self._prompt("chief_researcher")},
                          {"role": "user", "content": json.dumps(
                              {"analysis_id": analysis_id, "FACT DATA": immutable_facts,
                               "role_reports": chief_input}, ensure_ascii=False, default=str)}]
        chief = self.router.call("chief_researcher", chief_messages, ChiefReport, analysis_id, analysis_mode)
        return {"analysis_id": analysis_id, "analysis_mode": analysis_mode,
                "symbol": symbol, "fact_data": immutable_facts,
                "employees": {role: vars(result) for role, result in role_results.items()},
                "chief_researcher": vars(chief)}
