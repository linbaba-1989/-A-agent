"""Parallel AI workforce. Models interpret immutable QMT facts only."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .agent_schemas import (ChiefReport, FundamentalEventReport, RiskReport,
                            SentimentReport, TechnicalReport)
from .llm_router import LLMRouter, RouterResult
from .a_share_factors import build_role_factors
from .knowledge_loader import load_role_knowledge, structured_prompt

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
        role_text = (self.prompt_dir / PROMPT_FILES[role]).read_text(encoding="utf-8")
        return structured_prompt(role_text, load_role_knowledge(role))

    @staticmethod
    def _role_facts(role: str, facts: dict[str, Any]) -> dict[str, Any]:
        common_keys = ("fact_classification", "code", "name", "quote_time", "market_status",
                       "quote_type", "last_price", "previous_close", "open", "high", "low",
                       "volume", "amount", "change_pct", "source")
        common = {key: facts.get(key, "unavailable") for key in common_keys}
        role_keys = {
            "technical_analyst": ("recent_daily_k", "ma5", "ma10", "ma20", "ma60", "atr14",
                                   "volume_ratio", "turnover_rate", "high_5d", "high_10d", "high_20d",
                                   "breakout_status", "speed_1m", "speed_3m", "speed_5m"),
            "fundamental_event_analyst": ("fundamental_data", "event_data", "announcement_data",
                                           "news_data", "industry_data"),
            "sentiment_analyst": ("sentiment_external_data", "news_data", "volume_ratio",
                                   "turnover_rate", "speed_1m", "speed_3m", "speed_5m"),
            "risk_officer": ("recent_daily_k", "ma5", "ma10", "ma20", "ma60", "atr14",
                              "volume_ratio", "turnover_rate", "high_5d", "high_10d", "high_20d",
                              "intraday_position", "breakout_status", "security_status",
                              "fundamental_data", "event_data", "sentiment_external_data"),
        }[role]
        return {"confirmed_market_facts": common,
                "role_specific_data": {key: facts.get(key, "unavailable") for key in role_keys},
                "program_factors": build_role_factors(facts)}

    @staticmethod
    def analysis_id(symbol: str, analysis_mode: str = "standard", now: datetime | None = None) -> str:
        return f"{symbol}_{(now or datetime.now()).strftime('%Y%m%d_%H%M%S')}_{analysis_mode}"

    def _role_call(self, role: str, facts: dict[str, Any], analysis_id: str, analysis_mode: str) -> RouterResult:
        role_facts = self._role_facts(role, facts)
        messages = [{"role": "system", "content": self._prompt(role)},
                    {"role": "user", "content": "FACT DATA（只读）：\n" +
                     json.dumps(role_facts, ensure_ascii=False, default=str)}]
        return self.router.call(role, messages, ROLE_SCHEMAS[role], analysis_id, analysis_mode)

    def _timed_role_call(self, role: str, facts: dict[str, Any], analysis_id: str,
                         analysis_mode: str) -> tuple[RouterResult, str, str]:
        started = datetime.now().astimezone().isoformat(timespec="milliseconds")
        result = self._role_call(role, facts, analysis_id, analysis_mode)
        ended = datetime.now().astimezone().isoformat(timespec="milliseconds")
        return result, started, ended

    def analyze(self, symbol: str, fact_data: dict[str, Any], analysis_mode: str = "standard") -> dict[str, Any]:
        analysis_id = self.analysis_id(symbol, analysis_mode)
        immutable_facts = json.loads(json.dumps(fact_data, ensure_ascii=False, default=str))
        role_results: dict[str, RouterResult] = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai-employee") as pool:
            futures = {pool.submit(self._timed_role_call, role, immutable_facts, analysis_id, analysis_mode): role
                       for role in ROLE_SCHEMAS}
            for future in as_completed(futures):
                role = futures[future]
                try:
                    result, started, ended = future.result()
                    result.start_time = started
                    result.end_time = ended
                    role_results[role] = result
                except Exception as exc:
                    role_results[role] = RouterResult(role, None, None, False, None, 0.0,
                                                      error=f"{type(exc).__name__}: {exc}")
        successful = {role: result.data for role, result in role_results.items() if result.success}
        missing_roles = [role for role, result in role_results.items() if not result.success]
        if not successful:
            chief = RouterResult("chief_researcher", None, None, False, None, 0.0,
                                 error="insufficient_specialist_results", analysis_id=analysis_id,
                                 analysis_mode=analysis_mode, status="insufficient_specialist_results",
                                 usage_status="unavailable")
            return {"analysis_id": analysis_id, "analysis_mode": analysis_mode,
                    "symbol": symbol, "fact_data": immutable_facts,
                    "employees": {role: vars(result) for role, result in role_results.items()},
                    "chief_researcher": vars(chief)}
        chief_status = "degraded" if len(successful) == 1 else "complete"
        chief_messages = [{"role": "system", "content": self._prompt("chief_researcher")},
                          {"role": "user", "content": json.dumps(
                              {"analysis_id": analysis_id, "FACT DATA": immutable_facts,
                               "specialist_success_count": len(successful), "status": chief_status,
                               "missing_roles": missing_roles, "successful_role_reports": successful},
                              ensure_ascii=False, default=str)}]
        chief_started = datetime.now().astimezone().isoformat(timespec="milliseconds")
        chief = self.router.call("chief_researcher", chief_messages, ChiefReport, analysis_id, analysis_mode)
        chief.status = chief_status if chief.success else chief.status
        chief.start_time = chief_started
        chief.end_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
        return {"analysis_id": analysis_id, "analysis_mode": analysis_mode,
                "symbol": symbol, "fact_data": immutable_facts,
                "employees": {role: vars(result) for role, result in role_results.items()},
                "chief_researcher": vars(chief)}
