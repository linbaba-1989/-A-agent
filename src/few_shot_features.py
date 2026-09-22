"""Deterministic retrieval clues, never additions to the immutable Fact Bundle."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .a_share_factors import confidence_cap
from .agent_schemas import RESEARCH_TIME_HORIZONS

ROLES = ("technical_analyst", "fundamental_event_analyst", "sentiment_analyst",
         "risk_officer", "chief_researcher")
MARKET_FIELDS = ("market_breadth", "limit_up_count", "broken_board_rate",
                 "sector_strength", "leader_status")
DOMAIN_FIELDS = ("fundamental_data", "valuation_data", "announcement_data", "news_data", "industry_data")


def observed(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return None if value.get("status") == "unavailable" else value["value"]
    return value


def available(value: Any) -> bool:
    value = observed(value)
    if value is None: return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "unavailable", "unknown", "none", "--", "nan")
    if isinstance(value, (int, float)) and not isinstance(value, bool): return math.isfinite(value)
    if isinstance(value, dict):
        if value.get("status", value.get("data_status")) == "unavailable": return False
        return any(available(v) for k, v in value.items() if k not in ("status", "data_status"))
    if isinstance(value, (list, tuple)): return any(available(v) for v in value)
    return False


def number(value: Any) -> float | None:
    value = observed(value)
    if isinstance(value, bool): return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError): return None


@dataclass(frozen=True)
class ScenarioFeatures:
    role: str
    tags: frozenset[str]
    availability: dict[str, bool]
    values: dict[str, Any]
    trend_state: str
    conflicts: frozenset[str]
    evidence_patterns: frozenset[str]
    horizons: dict[str, str]


def scenario_features(role: str, facts: dict, specialists: dict | None = None) -> ScenarioFeatures:
    if role not in ROLES: raise ValueError("unknown_few_shot_role")
    values = {key: observed(value) for key, value in facts.items()}
    if "sector_strength" not in values:
        values["sector_strength"] = observed(facts.get("main_sector_strength"))
    avail = {key: available(value) for key, value in values.items()}
    for key in (*MARKET_FIELDS, *DOMAIN_FIELDS, "event_data", "sentiment_external_data"):
        avail.setdefault(key, False)
    tags, conflicts, evidence = set(), set(), set()
    price = number(values.get("last_price"))
    mas = {n: number(values.get(f"ma{n}")) for n in (5, 10, 20, 60)}
    for n, ma in mas.items():
        values[f"price_vs_ma{n}"] = ("above" if price > ma else "below" if price < ma else "equal") if (
            price is not None and ma is not None and price > 0 and ma > 0) else "unavailable"
    alignment = "unavailable"
    if all(v is not None and v > 0 for v in mas.values()):
        alignment = "bullish" if mas[5] > mas[10] > mas[20] > mas[60] else (
            "bearish" if mas[5] < mas[10] < mas[20] < mas[60] else "mixed")
    values["ma_alignment"] = alignment
    trend = values.get("trend_state")
    if trend not in ("strong_up", "uptrend", "range_up", "range", "range_down", "downtrend", "strong_down", "unclear"):
        above = lambda n: values[f"price_vs_ma{n}"] == "above"
        below = lambda n: values[f"price_vs_ma{n}"] == "below"
        trend = "strong_up" if all(above(n) for n in mas) and alignment == "bullish" else (
            "strong_down" if all(below(n) for n in mas) and alignment == "bearish" else (
                "uptrend" if above(20) and above(60) else "downtrend" if below(20) and below(60) else "unavailable"))
    if all(values[f"price_vs_ma{n}"] == "above" for n in (5, 10, 20)) and values["price_vs_ma60"] == "below":
        tags.add("short_up_mid_pressure"); conflicts.add("time_horizon")
    vr, change = number(values.get("volume_ratio")), number(values.get("change_pct"))
    turnover = number(values.get("turnover_rate"))
    atr = number(values.get("atr_pct"))
    if atr is None and price is not None and price > 0 and number(values.get("atr14")) is not None:
        atr = number(values["atr14"]) / price * 100
    values["atr_pct"] = atr
    if atr is not None and atr >= 5: tags.add("high_volatility")
    if turnover is not None and turnover >= 10: tags.add("crowding")
    position = number(values.get("range_position_20d"))
    if position is not None and position >= 80: tags.add("high_position")
    strong = trend in ("strong_up", "uptrend")
    if strong: tags.add("trend_up")
    if vr is not None and change is not None:
        if vr < 1 and change < 0:
            tags.add("low_volume_pullback")
            if values["price_vs_ma20"] == "above" and values["price_vs_ma60"] == "above":
                tags.add("strong_trend_pullback")
        if vr < 1 and change > 0: tags.add("low_volume_rise")
        if vr >= 1.5 and abs(change) <= 1: tags.add("volume_stagnation")
    high, low = number(values.get("high")), number(values.get("low"))
    reference = number(values.get("high_20d"))
    breakout = str(values.get("breakout_status", ""))
    attempt = price is not None and reference is not None and reference > 0 and price >= reference
    attempt = attempt or breakout in ("unconfirmed", "confirmed", "breakout", "未确认突破", "突破未确认", "5d", "10d", "20d")
    if attempt:
        tags.add("breakout_attempt")
        if breakout != "confirmed" or vr is None or vr < 1.5: tags.add("breakout_unconfirmed")
        if vr is not None and vr < 1: tags.add("low_volume_breakout")
        if vr is not None and vr >= 1.5: tags.add("volume_breakout")
    if high is not None and price is not None and reference is not None and high > reference > price:
        tags.add("false_breakout")
    if high is not None and low is not None and price is not None and high > low:
        location = (price - low) / (high - low)
        if location <= .3 and change is not None and change <= 0: tags.add("intraday_reversal_down")
        if location >= .7 and change is not None and change > 0: tags.add("intraday_reversal_up")
    if values["price_vs_ma20"] == "below": tags.add("ma_break_risk")
    if trend in ("downtrend", "strong_down") and change is not None and change > 0:
        tags.add("weak_rebound")
    if change is not None and change >= 5: tags.add("individual_strength")
    if "high_position" in tags and change is not None and change >= 5: tags.add("high_acceleration")
    amount = number(values.get("amount"))
    if amount is not None and 0 <= amount < 20_000_000: tags.add("low_liquidity")
    if values.get("at_limit_down") is True: tags.add("limit_down_exit")

    market_ready = all(avail[key] for key in MARKET_FIELDS)
    if not market_ready:
        tags.add("market_data_missing"); evidence.add("market_unavailable")
    phase = values.get("market_phase")
    if market_ready and phase in ("ice", "repair", "warming", "acceleration", "climax", "divergence", "retreat"):
        tags.add("phase_" + phase)
    # Missing market-wide inputs never turn a stock's turnover into a market phase.
    if strong and market_ready and phase in ("climax", "divergence"):
        tags.add("trend_hot_sentiment"); conflicts.add("sentiment_risk")
    if available(values.get("high_board_feedback")) and values["high_board_feedback"] == "negative":
        tags.add("leader_negative")
    sector = number(values.get("sector_strength"))
    if "individual_strength" in tags and sector is not None and sector < 0:
        tags.add("stock_sector_conflict"); conflicts.add("stock_sector")

    if not any(avail[key] for key in (*DOMAIN_FIELDS, "event_data")):
        tags.add("fundamental_missing"); evidence.add("fundamental_unavailable")
    if not avail["event_data"] and not avail["announcement_data"]: tags.add("event_gap")
    for key, tag in (("announcement_data", "announcement"), ("news_data", "news"),
                     ("industry_data", "industry"), ("official_data", "official")):
        if avail.get(key, False): tags.add(tag)
    for key in ("confirmed_events", "unconfirmed_events"):
        events = values.get(key)
        count = len(events) if isinstance(events, list) else 0
        values[key + "_count"] = count
        if count: tags.add(key)
    for key in ("contract_data", "revenue_data", "product_data", "order_data"):
        if avail.get(key, False): tags.add(key)
    if tags & {"unconfirmed_events", "news"}: evidence.add("unconfirmed_reporting")
    if "announcement" in tags: evidence.add("source_document")
    if "fundamental_missing" in tags and "market_data_missing" in tags:
        tags.add("data_gaps"); evidence.add("incomplete")
    if not any(available(values.get(k)) for k in ("last_price", "recent_daily_k", "ma20", *DOMAIN_FIELDS, *MARKET_FIELDS)):
        tags.add("severe_missing"); evidence.add("insufficient")

    # Specialist statements are MODEL_INFERENCE: used only as conflict clues for Chief.
    reports, unavailable_roles = {}, []
    for specialist, row in sorted((specialists or {}).items()):
        if row.get("success") and isinstance(row.get("data"), dict): reports[specialist] = row["data"]
        else: unavailable_roles.append(specialist)
    if unavailable_roles: tags.add("role_failure")
    values["unavailable_roles"] = unavailable_roles
    opinions = []
    for report in reports.values():
        short, mid = report.get("short_term_view"), report.get("mid_term_view")
        if short in ("bearish", "neutral_bearish") and mid in ("bullish", "neutral_bullish"):
            tags.add("short_down_mid_up"); conflicts.add("time_horizon")
        opinion = report.get("short_term_view", report.get("overall_view"))
        if opinion in ("bullish", "neutral_bullish"): opinions.append("up")
        if opinion in ("bearish", "neutral_bearish"): opinions.append("down")
    technical = reports.get("technical_analyst", {})
    risk = reports.get("risk_officer", {})
    technical_up = technical.get("trend_state", technical.get("trend")) in ("strong_up", "uptrend", "up") or technical.get("short_term_view") in ("bullish", "neutral_bullish")
    high_risk = risk.get("risk_level") in ("high", "very_high")
    if (strong and tags & {"high_volatility", "crowding"}) or (technical_up and high_risk):
        tags.add("trend_risk_conflict"); conflicts.add("technical_risk")
    if set(opinions) == {"up", "down"}:
        tags.add("role_disagreement"); conflicts.add("specialists")
    if any(report.get("evidence_quality") in ("low", "insufficient") for report in reports.values()):
        tags.add("evidence_conflict"); evidence.add("incomplete")
    if values.get("quote_status") in ("STALE", "CACHED"):
        tags.add("stale_evidence"); evidence.add("stale")
    cap = confidence_cap(facts)
    risk_cap = number(risk.get("max_confidence_allowed"))
    values["confidence_cap"] = min(cap, risk_cap) if risk_cap is not None and 0 <= risk_cap <= 100 else cap
    if values["confidence_cap"] <= 65: tags.add("low_confidence_cap")
    if values["price_vs_ma20"] == "above" and tags & {"high_volatility", "crowding"}:
        tags.add("protection_and_risk")
    return ScenarioFeatures(role, frozenset(tags), avail, values, str(trend), frozenset(conflicts),
                            frozenset(evidence), dict(RESEARCH_TIME_HORIZONS))
