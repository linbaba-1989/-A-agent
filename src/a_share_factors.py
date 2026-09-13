"""Deterministic A-share domain factors prepared before LLM interpretation."""
from __future__ import annotations

from typing import Any

UNAVAILABLE = "unavailable"


def _num(value: Any) -> float | None:
    try: return None if value in (None, UNAVAILABLE) else float(value)
    except (TypeError, ValueError): return None


def factor(value: Any, evidence: list[str] | None = None) -> dict[str, Any]:
    return {"value": value if value is not None else None,
            "status": "available" if value not in (None, UNAVAILABLE) else UNAVAILABLE,
            "evidence": evidence or []}


def technical_factors(facts: dict[str, Any]) -> dict[str, Any]:
    last = _num(facts.get("last_price"))
    mas = {key: _num(facts.get(key)) for key in ("ma5", "ma10", "ma20", "ma60")}
    evidence, score = [], 0
    for key, value in mas.items():
        if last is not None and value is not None:
            above = last > value; score += 1 if above else -1
            evidence.append(f"price {'>' if above else '<='} {key.upper()}")
    if all(mas[key] is not None for key in ("ma5", "ma10", "ma20")):
        if mas["ma5"] > mas["ma10"] > mas["ma20"]: score += 2; evidence.append("MA5>MA10>MA20")
        elif mas["ma5"] < mas["ma10"] < mas["ma20"]: score -= 2; evidence.append("MA5<MA10<MA20")
    trend = "unavailable" if last is None or len(evidence) < 2 else (
        "strong_up" if score >= 5 else "up" if score >= 2 else "strong_down" if score <= -5
        else "down" if score <= -2 else "neutral")
    return {"trend_state": factor(trend, evidence), "trend_score": factor(score, evidence),
            "position_20d": factor(facts.get("range_position_20d", UNAVAILABLE)),
            "atr_pct": factor(facts.get("atr_pct", UNAVAILABLE)),
            "turnover": factor(facts.get("turnover_rate", UNAVAILABLE)),
            "volume_ratio": factor(facts.get("volume_ratio", UNAVAILABLE)),
            "breakout_status": factor(facts.get("breakout_status", UNAVAILABLE))}


def sentiment_factors(facts: dict[str, Any]) -> dict[str, Any]:
    names = ("market_breadth", "limit_up_count", "limit_down_count", "broken_board_rate",
             "max_consecutive_limit_up", "promotion_rate", "yesterday_limit_up_premium",
             "high_board_feedback", "market_turnover", "turnover_change", "index_strength",
             "main_sector_strength", "money_effect", "loss_effect")
    market = {name: factor(facts.get(name, UNAVAILABLE)) for name in names}
    required = ("market_breadth", "limit_up_count", "limit_down_count", "broken_board_rate")
    market_phase = UNAVAILABLE if any(market[name]["status"] == UNAVAILABLE for name in required) else facts.get("market_phase", UNAVAILABLE)
    stock = {name: factor(facts.get(name, UNAVAILABLE)) for name in (
        "change_pct", "intraday_position", "turnover_rate", "volume_ratio", "speed_1m", "speed_3m",
        "speed_5m", "distance_to_recent_high", "distance_to_recent_low", "gap_behavior",
        "intraday_reversal", "close_strength", "sector_strength", "sector_rank", "leader_status")}
    crowd_inputs = [_num(facts.get(key)) for key in ("change_pct", "turnover_rate", "atr_pct", "range_position_20d")]
    available = [value for value in crowd_inputs if value is not None]
    crowding = UNAVAILABLE if len(available) < 3 else ("high" if sum(value > limit for value, limit in zip(crowd_inputs, (7, 10, 5, 80)) if value is not None) >= 2 else "medium")
    return {"market": market, "market_phase": factor(market_phase), "stock": stock,
            "crowding_level": factor(crowding, ["change/turnover/ATR/position"] if crowding != UNAVAILABLE else [])}


def risk_factors(facts: dict[str, Any]) -> dict[str, Any]:
    risks, protections = [], []
    atr, turnover, position = map(_num, (facts.get("atr_pct"), facts.get("turnover_rate"), facts.get("range_position_20d")))
    if atr is not None and atr >= 5: risks.append(f"ATR%={atr:.2f}, 波动较高")
    if turnover is not None and turnover >= 10: risks.append(f"换手率={turnover:.2f}%, 拥挤/兑现需验证")
    if position is not None and position >= 80: risks.append(f"20日位置={position:.2f}%, 高位风险")
    last, ma20 = _num(facts.get("last_price")), _num(facts.get("ma20"))
    if last is not None and ma20 is not None and last > ma20: protections.append("价格仍在MA20上方")
    gaps = [key for key in ("fundamental_data", "event_data", "sentiment_external_data")
            if facts.get(key, UNAVAILABLE) == UNAVAILABLE]
    return {"risk_factors": risks, "protective_factors": protections,
            "invalidation_conditions": ["收盘跌破MA20且成交放大"] if ma20 is not None else [],
            "data_gaps": gaps, "risk_level": "high" if len(risks) >= 2 else "medium" if risks else "low"}


def confidence_cap(facts: dict[str, Any]) -> int:
    missing_fundamental = facts.get("fundamental_data", UNAVAILABLE) == UNAVAILABLE
    missing_sentiment = all(facts.get(key, UNAVAILABLE) == UNAVAILABLE for key in
                            ("market_breadth", "limit_up_count", "broken_board_rate", "sector_strength"))
    return 65 if missing_fundamental and missing_sentiment else 80 if missing_fundamental or missing_sentiment else 95


def build_role_factors(facts: dict[str, Any]) -> dict[str, Any]:
    return {"technical_factors": technical_factors(facts), "sentiment_factors": sentiment_factors(facts),
            "risk_factors": risk_factors(facts),
            "fundamental_data_status": "unavailable" if facts.get("fundamental_data", UNAVAILABLE) == UNAVAILABLE else "available",
            "confidence_cap": confidence_cap(facts)}
