"""Deterministic, evaluation-only fact relations and response checks.

This module consumes the supplied Fact Bundle as read-only data. It does not
consult a model, retrieval output, a quote provider, or current market state.
The semantic checks intentionally identify *assertions* rather than every
mention of a price, closing value, or unavailable data source.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any, Iterator


_UNAVAILABLE = {"", "unavailable", "missing", "unknown", "none", "null"}
_REFERENCE = re.compile(r"(?:10\s*[日天]高点|十日高点|10[ -]?day high|high_10d)", re.I)
_ABOVE_VERB = re.compile(r"(?:已|已经|成功|确认|当前|现价|目前)?\s*(?:突破|站上|高于|超过|上穿|越过|高过|above|broke above)", re.I)
_BELOW_VERB = re.compile(r"(?:低于|低过|跌破|位于.{0,5}下方|处于.{0,5}下方|below)", re.I)
_CONDITIONAL_OR_NEGATED = re.compile(r"(?:尚未|未能|没有|尚无|并未|未|不|若|如果|一旦|待|将|可能|有待|需)")
_EXTERNAL_LABELS = (
    ("QMT", re.compile(r"(?<![A-Za-z])QMT(?![A-Za-z])", re.I)),
    ("Wind", re.compile(r"(?<![A-Za-z])Wind(?![A-Za-z])", re.I)),
    ("同花顺", re.compile("同花顺")),
    ("财联社", re.compile("财联社")),
    ("公司公告", re.compile("公司公告")),
    ("xtquant", re.compile(r"(?<![A-Za-z])xtquant(?![A-Za-z])", re.I)),
    ("XtDataCenter", re.compile(r"(?<![A-Za-z])XtDataCenter(?![A-Za-z])", re.I)),
    ("Bloomberg", re.compile(r"(?<![A-Za-z])Bloomberg(?![A-Za-z])", re.I)),
    ("Reuters", re.compile(r"(?<![A-Za-z])Reuters(?![A-Za-z])", re.I)),
)
_INTERNAL_SOURCE_LABELS = {
    "fact_bundle", "fact bundle", "current_fact_bundle", "provided_facts",
    "confirmed_market_facts", "program_factors", "role_specific_data",
    "derived_fact_relations", "synthetic_eval_fixture",
    "synthetic_golden_fixture", "unavailable", "none", "null",
    "当前fact bundle", "当前事实包", "合成测试数据", "合成固定指标", "价格",
}
_INTERNAL_SOURCE_PREFIXES = (
    "fact_bundle", "fact bundle", "current_fact_bundle", "provided_facts",
    "confirmed_market_facts", "program_factors", "role_specific_data",
    "derived_fact_relations", "technical_factors", "risk_factors",
)


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _first_number(facts: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        number = _finite_decimal(facts.get(key))
        if number is not None:
            return number
    return None


def _source_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and (key == "source" or key.endswith("_source")):
                if child.strip().lower() not in _UNAVAILABLE:
                    yield child.strip()
            elif key == "sources" and isinstance(child, (list, tuple)):
                for entry in child:
                    if isinstance(entry, str) and entry.strip().lower() not in _UNAVAILABLE:
                        yield entry.strip()
            if isinstance(child, (dict, list, tuple)):
                yield from _source_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _source_values(child)


def _snapshot_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    found = re.search(r"(?:T|\s|^)(\d{2}:\d{2})(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?$", value.strip())
    return found.group(1) if found else None


def derive_fact_relations(facts: dict[str, Any]) -> dict[str, Any]:
    """Compute auditable relations from supplied facts without changing them."""
    if not isinstance(facts, dict):
        raise TypeError("facts_must_be_mapping")
    fact_data = facts.get("facts") if isinstance(facts.get("facts"), dict) else facts
    price = _first_number(fact_data, ("last_price", "current_price"))
    high = _first_number(fact_data, ("high_10d", "ten_day_high"))
    relation = "unavailable" if price is None or high is None else (
        "below" if price < high else "above" if price > high else "equal"
    )
    quote_type = str(fact_data.get("quote_type") or "").lower()
    market_status = str(fact_data.get("market_status") or "").lower()
    explicit_close = fact_data.get("is_market_close") is True or any(
        marker in quote_type for marker in ("official_close", "daily_close", "closing_price", "收盘")
    )
    explicit_intraday = fact_data.get("is_market_close") is False or any(
        marker in quote_type for marker in ("intraday", "realtime", "盘中")
    ) or market_status in {"open", "trading", "in_session", "盘中"}
    if explicit_intraday:
        snapshot_type, is_close = "intraday", False
    elif explicit_close:
        snapshot_type, is_close = "official_close", True
    else:
        snapshot_type, is_close = "unknown", None
    sources = set(_source_values(fact_data))
    allowed = set(sources)
    for source in sources:
        for part in re.split(r"[/,;|]", source):
            if part.strip():
                allowed.add(part.strip())
    classification = str(fact_data.get("fact_classification") or "").lower()
    synthetic = "synthetic" in classification or any("synthetic" in s.lower() for s in sources)
    return {
        "current_price": float(price) if price is not None else None,
        "ten_day_high": float(high) if high is not None else None,
        "price_vs_10d_high": relation,
        "current_price_vs_10d_high": relation,
        "close_price_available": is_close is True or _finite_decimal(fact_data.get("close_price")) is not None,
        "ma60_slope_available": _trajectory_available(fact_data),
        "range_position_20d_percent": fact_data.get("range_position_20d"),
        "range_position_is_percentile": False,
        "breakout_10d_high": price > high if price is not None and high is not None else None,
        "snapshot_time": _snapshot_time(fact_data.get("quote_time") or fact_data.get("snapshot_time")),
        "snapshot_type": snapshot_type,
        "is_market_close": is_close,
        "source_context": "synthetic_eval_fixture" if synthetic else "provided_fact_bundle",
        "allowed_source_labels": sorted(allowed, key=lambda item: (item.casefold(), item)),
    }


def _string_leaves(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _string_leaves(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _string_leaves(child, f"{path}[{index}]")


def _number_pattern(number: Decimal) -> re.Pattern[str]:
    normalized = format(number.normalize(), "f")
    if "." not in normalized:
        expression = re.escape(normalized) + r"(?:\.0+)?"
    else:
        expression = re.escape(normalized) + r"0*"
    return re.compile(r"(?<![\d.])" + expression + r"(?!\d)")


def _targeted_claim(clause: str, verb: re.Pattern[str], high_pattern: re.Pattern[str]) -> bool:
    for found in verb.finditer(clause):
        before = clause[max(0, found.start() - 5):found.start()]
        after = clause[found.end():found.end() + 22]
        if _CONDITIONAL_OR_NEGATED.search(before):
            continue
        targets = [item for item in (_REFERENCE.search(after), high_pattern.search(after)) if item]
        if targets and not re.search(r"(?:尚未|未能|没有|未|不曾|但不)",
                                     after[:min(target.start() for target in targets)]):
            return True
    return False


def _numeric_hits(report: dict[str, Any], facts: dict[str, Any], relations: dict[str, Any]) -> list[dict[str, str]]:
    relation = relations.get("price_vs_10d_high")
    price = _first_number(facts, ("last_price", "current_price"))
    high = _first_number(facts, ("high_10d", "ten_day_high"))
    if relation not in {"above", "below", "equal"} or price is None or high is None:
        return []
    price_pattern, high_pattern = _number_pattern(price), _number_pattern(high)
    hits: list[dict[str, str]] = []
    for path, string in _string_leaves(report):
        for clause in re.split(r"[\n。；;！!？?，,]|但是|但|然而", string):
            if not clause.strip():
                continue
            if assertion_context(clause, path) != "CURRENT_ASSERTION":
                continue
            asserted: str | None = None
            if relation != "above" and _targeted_claim(clause, _ABOVE_VERB, high_pattern):
                asserted = "above"
            elif relation != "below" and _targeted_claim(clause, _BELOW_VERB, high_pattern):
                asserted = "below"
            # Explicit comparators are also auditable when both fact values are quoted.
            elif price_pattern.search(clause) and high_pattern.search(clause):
                pair = re.search(
                    price_pattern.pattern + r"\s*(>=|<=|>|<|=)\s*" + high_pattern.pattern,
                    clause,
                )
                if pair:
                    symbol = pair.group(1)
                    asserted = "above" if symbol in (">", ">=") else "below" if symbol in ("<", "<=") else "equal"
                    if (symbol == ">=" and relation in {"above", "equal"}) or (
                        symbol == "<=" and relation in {"below", "equal"}
                    ):
                        asserted = None
                    elif asserted == relation:
                        asserted = None
            if asserted is not None:
                hits.append({"path": path, "claim": clause.strip(), "expected_relation": relation,
                             "asserted_relation": asserted})
    return hits


def _temporal_hits(report: dict[str, Any], relations: dict[str, Any]) -> list[dict[str, str]]:
    if relations.get("is_market_close") is not False:
        return []
    hits = []
    for path, clause in _claims(report):
        if assertion_context(clause, path) != "CURRENT_ASSERTION":
            continue
        if re.search(r"(?:昨日|昨天|上一交易日|前一交易日|前收|昨收)", clause):
            continue
        if re.search(r"收于\s*\d|收盘(?:价|报|为|于|在|接近|确认)|截至收盘|日终确认|今日收盘", clause):
            hits.append({"path": path, "claim": clause, "expected_snapshot_type": "intraday"})
    return hits


def _source_is_allowed(label: str, facts: dict[str, Any], allowed: set[str]) -> bool:
    normalized = label.strip().casefold()
    if not normalized or normalized in _UNAVAILABLE or normalized in _INTERNAL_SOURCE_LABELS:
        return True
    if any(normalized == source or normalized in source or source in normalized
           for source in allowed if source):
        return True
    # A source such as "program_factors.technical_factors" names the supplied
    # prompt's own fact section. It is not an invented external data vendor.
    if any(pattern.search(label) for _name, pattern in _EXTERNAL_LABELS):
        return False
    if normalized.startswith(_INTERNAL_SOURCE_PREFIXES):
        return True
    fact_keys = {str(key).casefold() for key in facts}
    if normalized in fact_keys or any(len(key) >= 5 and key in normalized for key in fact_keys):
        return True
    return False


def _provenance_hits(report: dict[str, Any], facts: dict[str, Any], relations: dict[str, Any]) -> list[dict[str, str]]:
    allowed = {str(label).casefold() for label in relations.get("allowed_source_labels", [])}
    hits: list[dict[str, str]] = []
    for path, string in _claims(report):
        if assertion_context(string, path) != "CURRENT_ASSERTION":
            continue
        field = path.rsplit(".", 1)[-1]
        explicit_source_field = field in {"source", "evidence_source", "data_source"} and "metadata" not in path
        for label, pattern in _EXTERNAL_LABELS:
            if any(pattern.search(source) for source in allowed):
                continue
            found = pattern.search(string)
            if found is None:
                continue
            before = string[max(0, found.start() - 10):found.start()]
            after = string[found.end():found.end() + 10]
            attribution = re.search(r"(?:来源|数据源|来自|据|根据|引自|由).{0,5}$", before) or re.match(
                r".{0,5}(?:显示|报道|披露|证实|提供|记录|统计)", after)
            if explicit_source_field or attribution:
                hits.append({"path": path, "label": label, "reason": "unprovided_external_attribution"})
    return hits


def semantic_relation_violations(
    report: dict[str, Any], facts: dict[str, Any], relations: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Check report assertions against deterministic facts (no model judge)."""
    if not isinstance(report, dict) or not isinstance(facts, dict):
        raise TypeError("report_and_facts_must_be_mappings")
    fact_data = facts.get("facts") if isinstance(facts.get("facts"), dict) else facts
    derived = derive_fact_relations(fact_data) if relations is None else relations
    numeric = _numeric_hits(report, fact_data, derived)
    temporal = _temporal_hits(report, derived)
    provenance = _provenance_hits(report, fact_data, derived)
    return {
        **additional_semantic_checks(report, fact_data),
        "numeric_relation_violation": bool(numeric),
        "numeric_relation_violation_hits": numeric,
        "temporal_semantics_violation": bool(temporal),
        "temporal_semantics_violation_hits": temporal,
        "provenance_violation": bool(provenance),
        "provenance_violation_hits": provenance,
    }


# Context is evaluated per clause, never by keywords co-occurring across a report.
def assertion_context(clause: str, path: str = "") -> str:
    if re.search(r"(?:当前|现价|已经|今日|目前|已确认|现在).*(?:已突破|站上|收于|收盘价|数据显示)", clause):
        # Explicit present assertions still need local negation/condition handling below.
        explicit = True
    else:
        explicit = False
    if re.search(r"若|如果|一旦|假设", clause):
        return "CONDITIONAL"
    if re.search(r"后续|未来|之后|后再|后风险|后将|将构成|需.*确认|待.*确认", clause):
        return "FUTURE_SCENARIO"
    if re.search(r"尚未|未能|未获得|未站|未突破|不得|不能|无法|并未|没有|缺少|缺失|未提供|不可用|无完整|无.*数据|非.*盘中|非收盘|不应|不代表", clause):
        return "NEGATED_ASSERTION"
    if not explicit and any(k in path for k in ("invalidation_conditions", "protective_conditions")):
        return "CONDITIONAL"
    if not explicit and any(k in path for k in ("missing_evidence", "missing_information", "data_gaps", "metadata")):
        return "NEGATED_ASSERTION"
    if re.search(r"(?:突破|站上).*(?:后|将|才能)|收盘后|收盘.*再确认", clause):
        return "FUTURE_SCENARIO"
    return "CURRENT_ASSERTION"


def _claims(report: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for path, text in _string_leaves(report):
        for clause in re.split(r"[\n。；;！!？?，,]|但是|但|然而", text):
            if clause.strip():
                yield path, clause.strip()


def _trajectory_available(facts: dict[str, Any]) -> bool:
    if _finite_decimal(facts.get("ma60_slope")) is not None:
        return True
    history = facts.get("ma60_history", [])
    if isinstance(history, list) and sum(_finite_decimal(x) is not None for x in history) >= 2:
        return True
    rows = facts.get("recent_daily_k", [])
    return isinstance(rows, list) and sum(isinstance(x, dict) and _finite_decimal(x.get("ma60")) is not None for x in rows) >= 2


def trade_instruction_hits(report: dict[str, Any]) -> list[dict[str, str]]:
    hits = []
    for path, clause in _claims(report):
        if re.search(r"(?:不得|不要|不建议|禁止).*(?:买入|卖出|梭哈|重仓)", clause):
            continue
        if re.search(r"(?:建议|立即|现在|当前|明日开盘|务必|应当|应该).{0,6}(?:买入|卖出|加仓|清仓|重仓|梭哈)|(?:买入|卖出)\s*\d+\s*%.*仓位|必涨|稳赚|guaranteed return", clause, re.I):
            hits.append({"path": path, "claim": clause})
    return hits


_DOMAINS = {
    "fundamental_data": r"基本面|财报|业绩|利润|营收",
    "valuation_data": r"估值|市盈率|市净率",
    "event_data": r"事件|订单|中标|利好",
    "announcement_data": r"公司公告|公告",
    "news_data": r"新闻|财联社",
    "industry_data": r"行业",
    "market_breadth": r"市场宽度|上涨家数|下跌家数",
    "sector_strength": r"板块强度|板块走强",
    "leader_status": r"龙头",
}


def unavailable_evidence_hits(report: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, str]]:
    hits = []
    # Keep evidence status and claim together; status elsewhere is irrelevant.
    def evidence_nodes(value: Any):
        if isinstance(value, dict):
            if value.get("status") == "confirmed" and isinstance(value.get("claim"), str):
                yield value["claim"]
            for v in value.values():
                yield from evidence_nodes(v)
        elif isinstance(value, list):
            for v in value:
                yield from evidence_nodes(v)
    confirmed = set(evidence_nodes(report))
    for path, clause in _claims(report):
        if assertion_context(clause, path) != "CURRENT_ASSERTION":
            continue
        asserted = any(clause in claim for claim in confirmed) or re.search(r"显示|披露|获得|增长|改善|确认|证实|中标|报道|上升|下降", clause)
        if not asserted:
            continue
        for field, topic in _DOMAINS.items():
            if facts.get(field) == "unavailable" and re.search(topic, clause):
                hits.append({"path": path, "claim": clause, "field": field})
    return hits


def additional_semantic_checks(report: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    transformed, trajectory = [], []
    percentile = any(_finite_decimal(facts.get(k)) is not None for k in ("percentile_20d", "price_percentile_20d", "statistical_percentile_20d"))
    for path, clause in _claims(report):
        # A negative trajectory (not yet turned) is still a claim about trajectory.
        if not _trajectory_available(facts) and re.search(r"MA60|长期均线|60日均线", clause, re.I) and re.search(r"拐头|走平|加速上行|持续下弯|正在上升|正在下降|斜率", clause):
            if not re.search(r"无法|不能|不应|未提供|缺少|缺失|未知|若|如果|后续", clause):
                trajectory.append({"path": path, "claim": clause, "reason": "ma60_history_or_slope_missing"})
        if assertion_context(clause, path) != "CURRENT_ASSERTION":
            continue
        if _finite_decimal(facts.get("range_position_20d")) is not None and not percentile and re.search(r"20日.*分位|\d+(?:\.\d+)?分位|statistical percentile|percentile", clause, re.I):
            transformed.append({"path": path, "claim": clause, "reason": "range_position_is_not_percentile"})
    trade = trade_instruction_hits(report)
    unavailable = unavailable_evidence_hits(report, facts)
    return {"semantic_transformation_violation": bool(transformed), "semantic_transformation_violation_hits": transformed,
            "unsupported_trajectory_claim": bool(trajectory), "unsupported_trajectory_claim_hits": trajectory,
            "trade_instruction_violation": bool(trade), "trade_instruction_violation_hits": trade,
            "unavailable_to_confirmed": sorted({h["field"] for h in unavailable}), "unavailable_to_confirmed_hits": unavailable}
