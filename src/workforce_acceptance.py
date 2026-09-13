"""End-to-end AI workforce acceptance using one immutable QMT fact bundle."""
from __future__ import annotations

from datetime import datetime, time
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any

from .agent import StockResearchAgent
from .indicators import UNAVAILABLE, intraday_position, percent_change
from .market_cache import (daily_suspend_flag, history_indicators, normalize_instrument,
                           realtime_ma, security_status, validated_turnover)
from .qmt_provider import QMTProvider
from .scanner import MarketScanner

CORE_NUMBER_FIELDS = (
    "last_price", "previous_close", "change_pct", "amount", "turnover_rate",
    "ma5", "ma10", "ma20", "ma60",
)


def _amplitude(tick: dict[str, Any]) -> float | str:
    try:
        previous = float(tick["lastClose"])
        return round((float(tick["high"]) - float(tick["low"])) / previous * 100, 4) if previous > 0 else UNAVAILABLE
    except (KeyError, TypeError, ValueError):
        return UNAVAILABLE


def _market_state(timestamp: float | None, now: datetime | None = None) -> tuple[str, str]:
    if timestamp is None:
        return "closed", "latest_available_snapshot"
    quote_at = datetime.fromtimestamp(timestamp).astimezone()
    current = (now or datetime.now().astimezone())
    clock = quote_at.time().replace(tzinfo=None)
    continuous = time(9, 30) <= clock <= time(11, 30) or time(13, 0) <= clock <= time(15, 0)
    live = quote_at.date() == current.date() and current.weekday() < 5 and continuous
    return ("open", "realtime_snapshot") if live else ("closed", "latest_available_snapshot")


def build_fact_bundle(symbol: str = "600498.SH", provider: QMTProvider | None = None,
                      scanner: MarketScanner | None = None) -> dict[str, Any]:
    """Read QMT once and create the immutable input shared by every role and mode."""
    provider = provider or QMTProvider()
    scanner = scanner or MarketScanner(provider)
    tick = provider.get_full_ticks([symbol]).get(symbol)
    if not tick or not provider._valid_tick(tick):
        raise RuntimeError(f"qmt_quote_unavailable: {symbol}")
    instrument = normalize_instrument(symbol, provider.get_instrument_detail(symbol))
    timestamp = provider.tick_timestamp(tick)
    as_of = datetime.fromtimestamp(timestamp) if timestamp else None
    history_frame = provider.get_local_history([symbol], 65).get(symbol)
    if history_frame is None:
        raise RuntimeError(f"qmt_history_unavailable: {symbol}")
    history = history_indicators(history_frame, as_of)
    suspend_flag = daily_suspend_flag(history_frame, as_of)
    turnover = validated_turnover(tick, instrument)
    last = tick.get("lastPrice", UNAVAILABLE)
    closes = history.get("closes", [])
    market_status, quote_type = _market_state(timestamp)
    high5, high10, high20 = (history.get("high_5d", UNAVAILABLE),
                             history.get("high_10d", UNAVAILABLE), history.get("high_20d", UNAVAILABLE))
    crossed = [label for label, value in (("5d", high5), ("10d", high10), ("20d", high20))
               if value != UNAVAILABLE and float(last) >= float(value)]
    complete_history = history_frame.copy()
    recent_columns = [column for column in ("time", "open", "high", "low", "close", "volume", "amount")
                      if column in complete_history.columns]
    recent_daily = complete_history[recent_columns].tail(20).to_dict(orient="records")
    volume_ratio = MarketScanner._volume_ratio(tick, history)
    return {
        "fact_classification": "CONFIRMED_FACT",
        "code": symbol,
        "name": instrument.get("name") or UNAVAILABLE,
        "quote_time": provider.normalize_tick(symbol, tick)["timestamp"],
        "market_status": market_status,
        "quote_type": quote_type,
        "last_price": last,
        "previous_close": tick.get("lastClose", UNAVAILABLE),
        "open": tick.get("open", UNAVAILABLE),
        "high": tick.get("high", UNAVAILABLE),
        "low": tick.get("low", UNAVAILABLE),
        "volume": tick.get("volume", UNAVAILABLE),
        "amount": tick.get("amount", UNAVAILABLE),
        "change_pct": percent_change(last, tick.get("lastClose")),
        "amplitude": _amplitude(tick),
        "turnover_rate": turnover["value"],
        "ma5": realtime_ma(closes, last, 5),
        "ma10": realtime_ma(closes, last, 10),
        "ma20": realtime_ma(closes, last, 20),
        "ma60": realtime_ma(closes, last, 60),
        "atr14": history.get("atr14", UNAVAILABLE),
        "volume_ratio": volume_ratio,
        "recent_daily_k": recent_daily,
        "high_5d": high5,
        "high_10d": high10,
        "high_20d": high20,
        "intraday_position": intraday_position(last, tick.get("low"), tick.get("high")),
        "breakout_status": ",".join(crossed) if crossed else "no_confirmed_breakout",
        "security_status": security_status(tick, suspend_flag),
        "speed_1m": scanner.snapshot_history.speed(symbol, 1),
        "speed_3m": scanner.snapshot_history.speed(symbol, 3),
        "speed_5m": scanner.snapshot_history.speed(symbol, 5),
        "fundamental_data": UNAVAILABLE,
        "event_data": UNAVAILABLE,
        "announcement_data": UNAVAILABLE,
        "news_data": UNAVAILABLE,
        "industry_data": UNAVAILABLE,
        "sentiment_external_data": UNAVAILABLE,
        "source": "QMT/xtquant",
    }


def unsupported_numeric_claims(report: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Flag precise numbers in prose which cannot be traced to the supplied facts."""
    supported = []
    for value in facts.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            supported.append(float(value))
    structural = {1.0, 3.0, 5.0, 10.0, 14.0, 20.0, 60.0, 100.0}
    claims: list[dict[str, Any]] = []

    def inspect(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "confidence":
                    continue
                inspect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}[{index}]")
        elif isinstance(value, str):
            for raw in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", value.replace(",", "")):
                number = float(raw)
                if number in structural or any(abs(number - item) <= max(1e-6, abs(item) * 1e-5) for item in supported):
                    continue
                claims.append({"path": path, "value": raw, "text": value})

    inspect(report, "report")
    return claims


def _parallel_check(employees: dict[str, dict[str, Any]]) -> dict[str, Any]:
    intervals = []
    for role, result in employees.items():
        if not result.get("start_time") or not result.get("end_time"):
            return {"pass": False, "reason": f"missing_timing:{role}"}
        intervals.append((datetime.fromisoformat(result["start_time"]),
                          datetime.fromisoformat(result["end_time"])))
    overlap_seconds = (min(end for _, end in intervals) - max(start for start, _ in intervals)).total_seconds()
    return {"pass": overlap_seconds > 0, "shared_overlap_seconds": max(0.0, overlap_seconds)}


def _usage(result: dict[str, Any]) -> dict[str, Any]:
    rows = list(result["employees"].values()) + [result["chief_researcher"]]
    return {
        "analysis_id": result["analysis_id"],
        "analysis_mode": result["analysis_mode"],
        "roles": [{key: row.get(key) for key in (
            "role", "provider", "model", "reasoning_effort", "start_time", "end_time", "latency",
            "input_tokens", "output_tokens", "total_tokens", "estimated_cost", "success", "fallback",
            "error", "requested_model", "actual_model", "fallback_reason",
        )} for row in rows],
        "total_tokens": sum((row.get("total_tokens") or 0) for row in rows),
        "total_estimated_cost": sum(row.get("estimated_cost", 0.0) for row in rows),
    }


def run_acceptance(symbol: str = "600498.SH", output_root: str | Path = "outputs/acceptance/600498",
                   provider: QMTProvider | None = None, agent: StockResearchAgent | None = None) -> dict[str, Any]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    facts = build_fact_bundle(symbol, provider)
    (root / "fact_bundle.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    workforce = agent or StockResearchAgent()
    runs: dict[str, dict[str, Any]] = {}
    for mode in ("standard", "max"):
        started = perf_counter()
        result = workforce.analyze(symbol, facts, mode)
        elapsed = perf_counter() - started
        mode_dir = root / mode
        mode_dir.mkdir(exist_ok=True)
        for role, row in result["employees"].items():
            short = {"technical_analyst": "technical", "fundamental_event_analyst": "fundamental",
                     "sentiment_analyst": "sentiment", "risk_officer": "risk"}[role]
            (mode_dir / f"{short}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        (mode_dir / "chief.json").write_text(json.dumps(result["chief_researcher"], ensure_ascii=False, indent=2), encoding="utf-8")
        usage = _usage(result)
        usage["elapsed_seconds"] = elapsed
        usage["parallel_check"] = _parallel_check(result["employees"])
        (mode_dir / "usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
        claims = []
        for role, row in result["employees"].items():
            claims.extend({"role": role, **claim} for claim in unsupported_numeric_claims(row.get("data") or {}, facts))
        claims.extend({"role": "chief_researcher", **claim} for claim in
                      unsupported_numeric_claims(result["chief_researcher"].get("data") or {}, facts))
        runs[mode] = {"result": result, "usage": usage, "unsupported_numeric_claims": claims}
    standard, maximum = runs["standard"], runs["max"]
    comparison = {
        "same_fact_bundle": standard["result"]["fact_data"] == maximum["result"]["fact_data"] == facts,
        "standard": {"elapsed_seconds": standard["usage"]["elapsed_seconds"],
                     "total_tokens": standard["usage"]["total_tokens"],
                     "total_estimated_cost": standard["usage"]["total_estimated_cost"],
                     "chief_confidence": (standard["result"]["chief_researcher"].get("data") or {}).get("confidence")},
        "max": {"elapsed_seconds": maximum["usage"]["elapsed_seconds"],
                "total_tokens": maximum["usage"]["total_tokens"],
                "total_estimated_cost": maximum["usage"]["total_estimated_cost"],
                "chief_confidence": (maximum["result"]["chief_researcher"].get("data") or {}).get("confidence")},
        "unsupported_numeric_claim_count": sum(len(run["unsupported_numeric_claims"]) for run in runs.values()),
        "unsupported_numeric_claims": {mode: run["unsupported_numeric_claims"] for mode, run in runs.items()},
    }
    (root / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"fact_bundle": facts, "runs": runs, "comparison": comparison}
