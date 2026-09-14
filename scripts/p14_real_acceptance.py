"""Run exactly one paid P1.4 STANDARD E2E, then audit locally."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

from src.a_share_evals import evaluate_output, hallucination_claims
from src.a_share_factors import build_role_factors, confidence_cap
from src.agent import StockResearchAgent
from src.market_data_router import MarketDataRouter
from src.qmt_provider import QMTProvider
from src.scanner import MarketScanner
from src.workforce_acceptance import build_fact_bundle


OUTPUT = Path("outputs/acceptance")
SYMBOL = "600498.SH"


def compact_result(row: dict) -> dict:
    keys = ("role", "provider", "model", "actual_model", "success", "data", "latency", "input_tokens",
            "output_tokens", "total_tokens", "estimated_cost", "fallback", "fallback_reason", "error",
            "schema_repair_count", "reasoning_effort", "status")
    return {key: row.get(key) for key in keys}


def main() -> None:
    load_dotenv()
    router = MarketDataRouter(qmt_factory=lambda: QMTProvider())
    selection = router.select()
    if selection.provider is None:
        raise RuntimeError("market_data_unavailable")
    provider = selection.provider
    scanner = MarketScanner(provider)
    try:
        facts = build_fact_bundle(SYMBOL, provider=provider, scanner=scanner)
        recent = facts.get("recent_daily_k") or []
        lows = [float(row["low"]) for row in recent if row.get("low") is not None]
        low20 = min(lows) if lows else "unavailable"
        last, high20, atr = facts["last_price"], facts["high_20d"], facts["atr14"]
        facts["low_20d"] = low20
        facts["atr_pct"] = round(float(atr) / float(last) * 100, 2)
        facts["range_position_20d"] = (round((float(last) - float(low20)) / (float(high20) - float(low20)) * 100, 2)
                                               if high20 != low20 else "unavailable")
        if facts.get("breakout_status") == "no_confirmed_breakout":
            facts["breakout_status"] = "未确认突破"
        factors = build_role_factors(facts)
        cap = confidence_cap(facts)
        started = perf_counter()
        # The only paid E2E call in this script.
        result = StockResearchAgent().analyze(SYMBOL, facts, "standard")
        elapsed = perf_counter() - started
    finally:
        router.close()

    roles = {role: compact_result(row) for role, row in result["employees"].items()}
    roles["chief_researcher"] = compact_result(result["chief_researcher"])
    hallucinations = {role: hallucination_claims(row.get("data") or {}) for role, row in roles.items()}
    evaluations = {role: evaluate_output({**(row.get("data") or {}), "evidence":
                                           (row.get("data") or {}).get("confirmed_market_signals", []) or
                                           (row.get("data") or {}).get("confirmed_facts", []),
                                           "data_gaps": (row.get("data") or {}).get("data_gaps", []) or
                                           (row.get("data") or {}).get("missing_data", []) or
                                           (row.get("data") or {}).get("missing_sentiment_data", [])}, facts)
                   for role, row in roles.items()}
    chief_confidence = (roles["chief_researcher"].get("data") or {}).get("confidence")
    summary = {key: facts.get(key) for key in ("code", "name", "quote_time", "last_price", "change_pct",
                                                "ma5", "ma10", "ma20", "ma60", "atr14", "atr_pct",
                                                "high_20d", "low_20d", "range_position_20d",
                                                "turnover_rate", "volume_ratio", "breakout_status")}
    evidence = {"analysis_id": result["analysis_id"], "analysis_mode": "standard", "symbol": SYMBOL,
                "fact_bundle_summary": summary, "fact_bundle_hash": hashlib.sha256(
                    json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
                "program_factors": factors, "confidence_cap": cap, "roles": roles,
                "e2e_elapsed_seconds": elapsed}
    audit = {"analysis_id": result["analysis_id"], "confidence_cap": cap,
             "chief_confidence": chief_confidence,
             "confidence_cap_violation": isinstance(chief_confidence, (int, float)) and chief_confidence > cap,
             "hallucination_audit": hallucinations,
             "hallucination_count": sum(len(items) for items in hallucinations.values()),
             "eval": evaluations, "e2e_elapsed_seconds": elapsed}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "p14_600498_standard_real.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUTPUT / "p14_600498_standard_eval.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"saved": True, "analysis_id": result["analysis_id"], "success":
                      {role: row["success"] for role, row in roles.items()}, "elapsed": elapsed,
                      "chief_confidence": chief_confidence, "confidence_cap": cap,
                      "hallucination_count": audit["hallucination_count"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
