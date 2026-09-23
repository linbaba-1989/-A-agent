"""Technical-only compatibility, derived clues never written into FACT DATA."""
from .few_shot_features import number

AXES = ("short_term_state", "mid_term_state", "trend_state", "ma5_relation",
        "ma10_relation", "ma20_relation", "ma60_relation", "volume_state", "breakout_state")
UP = {"bullish", "neutral_bullish", "uptrend", "strong_up"}
DOWN = {"bearish", "neutral_bearish", "downtrend", "strong_down"}
FLAT = {"neutral", "range", "range_up", "range_down"}


def technical_dimensions(features):
    v = features.values
    result = {axis: "unknown" for axis in AXES}
    for n in (5, 10, 20, 60):
        result[f"ma{n}_relation"] = v.get(f"price_vs_ma{n}", "unknown")
    change = number(v.get("change_pct"))
    if change is not None:
        result["short_term_state"] = "bearish" if change < 0 else "bullish" if change > 0 else "neutral"
    mas = [number(v.get(f"ma{n}")) for n in (5, 10, 20)]
    # MA20 structure (2-8 week research horizon) and overhead MA60 are separate axes.
    if all(ma is not None and ma > 0 for ma in mas):
        if mas[0] > mas[1] > mas[2] and result["ma20_relation"] == "above":
            result["mid_term_state"] = "uptrend"
        elif mas[0] < mas[1] < mas[2] and result["ma20_relation"] == "below":
            result["mid_term_state"] = "downtrend"
    explicit_mid = v.get("mid_term_state")
    if explicit_mid in UP | DOWN | FLAT:
        result["mid_term_state"] = explicit_mid
    result["trend_state"] = features.trend_state
    if result["trend_state"] == "unavailable":
        result["trend_state"] = result["mid_term_state"]
    vr = number(v.get("volume_ratio"))
    if vr is not None:
        result["volume_state"] = "low" if vr < 1 else "high" if vr >= 1.5 else "normal"
    breakout = v.get("breakout_status")
    if breakout in ("unconfirmed", "no_confirmed_breakout", "未确认突破", "突破未确认"):
        result["breakout_state"] = "unconfirmed"
    elif breakout == "confirmed":
        result["breakout_state"] = "confirmed"
    return result


def relation(current, expected):
    if expected == "any":
        return "neutral"
    if current in ("unknown", "unavailable", "unclear", None) or expected in ("unknown", None):
        return "unknown"
    expected = expected if isinstance(expected, (tuple, list)) else [expected]
    if current in expected or any(current in group and set(expected) & group for group in (UP, DOWN, FLAT)):
        return "match"
    return "conflict"


def technical_conflict(case, features):
    current = technical_dimensions(features)
    context = case.technical_context
    dimensions = {axis: relation(current[axis], context.get(axis)) for axis in AXES}
    # Declared legacy trend constraints also apply; absent metadata is not a match.
    if "trend_state" not in context and case.trend_states:
        dimensions["trend_state"] = relation(current["trend_state"], case.trend_states)
    critical = [axis for axis in ("mid_term_state", "trend_state") if dimensions[axis] == "conflict"]
    mandatory = [axis for axis in context.get("mandatory_axes", [])
                 if dimensions.get(axis) in ("conflict", "unknown")]
    hard = bool(critical or mandatory)
    # A pressure-framed case without a stated middle-horizon direction is unsafe
    # as a leading example when MA20 structure is up but MA60 is overhead.
    ambiguous_pressure = (context.get("ma60_context") == "mid_pressure" and
        current["mid_term_state"] in UP and current["ma60_relation"] == "below" and
        dimensions["mid_term_state"] == "unknown")
    penalty = 100.0 if ambiguous_pressure and not hard else 0.0
    return {"case_id": case.case_id, "current_dimensions": current, "dimensions": dimensions,
            "critical_feature_conflict": bool(critical or mandatory or ambiguous_pressure),
            "conflict_certainty": "explicit" if critical or mandatory else "potential" if ambiguous_pressure else "none",
            "action": "hard_exclusion" if hard else "contradiction_penalty" if penalty else "compatible",
            "penalty": penalty, "reasons": [*critical, *mandatory] +
                (["mid_horizon_metadata_insufficient_with_ma60_pressure"] if ambiguous_pressure else [])}
