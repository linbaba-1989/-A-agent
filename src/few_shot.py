"""Role-isolated local case retrieval, disabled by default. No embedding/API calls."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path

from .agent_schemas import (ChiefReport, FundamentalEventReport, RiskReport,
                            SentimentReport, TechnicalReport, RESEARCH_TIME_HORIZONS)
from .few_shot_features import MARKET_FIELDS, ROLES, ScenarioFeatures, scenario_features

CASE_FILES = dict(zip(ROLES, ("technical_cases.jsonl", "fundamental_cases.jsonl",
                             "sentiment_cases.jsonl", "risk_cases.jsonl", "chief_cases.jsonl")))
SCHEMAS = dict(zip(ROLES, (TechnicalReport, FundamentalEventReport, SentimentReport, RiskReport, ChiefReport)))
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "knowledge" / "cases"
MIN_SCORE = 6.0
MAX_CASES = 3
TOKEN_BUDGET = 1000
RETRIEVER_VERSION = "p1.9.2-v1"
WEIGHTS = {role: (4., 1., 2., 4., 2.) for role in ROLES}  # tags/data/trend/conflict/evidence
WEIGHTS["technical_analyst"] = (5., 1., 3., 4., 1.)
WEIGHTS["chief_researcher"] = (4., 1., 2., 6., 3.)
WEIGHTS["sentiment_analyst"] = (5., 1., 1., 3., 3.)


@dataclass(frozen=True)
class ResearchCase:
    case_id: str
    role: str
    title: str
    scenario_tags: tuple[str, ...]
    market_context: dict
    facts: dict
    reasoning_pattern: str
    expected_output_pattern: str
    data_requirements: dict[str, bool]
    anti_patterns: tuple[str, ...]
    required_tags: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    trend_states: tuple[str, ...] = ()
    signal_conflicts: tuple[str, ...] = ()
    evidence_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseLibrary:
    cases: dict[str, tuple[ResearchCase, ...]]
    errors: dict[str, str]
    version: str


def _rows(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]


def _case(data: dict, role: str) -> ResearchCase:
    if data.get("role") != role: raise ValueError("case_role_mismatch")
    required = ("case_id", "title", "reasoning_pattern", "expected_output_pattern")
    if any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
        raise ValueError("missing_case_text")
    if any(not isinstance(data.get(key), dict) for key in ("market_context", "facts", "data_requirements")):
        raise ValueError("invalid_case_object")
    if any(type(v) is not bool for v in data["data_requirements"].values()): raise ValueError("invalid_requirements")
    arrays = ("scenario_tags", "anti_patterns", "required_tags", "required_any", "trend_states", "signal_conflicts", "evidence_patterns")
    for key in arrays:
        if not isinstance(data.get(key, []), list) or any(not isinstance(v, str) for v in data.get(key, [])):
            raise ValueError("invalid_case_array")
    if not data.get("scenario_tags") or not (data.get("required_tags") or data.get("required_any")):
        raise ValueError("case_needs_feature_gate")
    return ResearchCase(**{key: data[key] for key in required}, role=role,
                        **{key: data[key] for key in ("market_context", "facts", "data_requirements")},
                        **{key: tuple(data.get(key, [])) for key in arrays})


def load_library(root: Path = DEFAULT_ROOT) -> CaseLibrary:
    """A frozen per-research snapshot; bad role files cannot contaminate other roles."""
    raw, read_errors = {}, {}
    names = (*CASE_FILES.values(), "retrieval_profiles.json", "retrieval_supplements.jsonl")
    for name in names:
        try:
            raw[name] = (root / name).read_bytes()
            if len(raw[name]) > 2_000_000: raise ValueError("case_file_too_large")
        except (OSError, ValueError): read_errors[name] = "case_file_unavailable"
    digest = sha256(RETRIEVER_VERSION.encode())
    for name in sorted(names):
        digest.update(name.encode()); digest.update(raw.get(name, b"missing"))
    digest.update(json.dumps(RESEARCH_TIME_HORIZONS, sort_keys=True).encode())
    # The policy/code participates, so a feature or scoring change is auditable too.
    for code in (Path(__file__), Path(__file__).with_name("few_shot_features.py")):
        digest.update(code.read_bytes())
    cases, errors = {}, {}
    try:
        profiles = json.loads(raw["retrieval_profiles.json"])
        if not isinstance(profiles, dict): raise ValueError("invalid_profiles")
    except (KeyError, ValueError, UnicodeError):
        return CaseLibrary({}, dict.fromkeys(ROLES, "profiles_unavailable"), digest.hexdigest())
    try:
        supplements = _rows(raw["retrieval_supplements.jsonl"])
        if any(not isinstance(row, dict) or row.get("role") not in ROLES for row in supplements):
            raise ValueError("invalid_supplement_role")
    except (KeyError, ValueError, UnicodeError):
        return CaseLibrary({}, dict.fromkeys(ROLES, "supplements_unavailable"), digest.hexdigest())
    for role, filename in CASE_FILES.items():
        try:
            if filename in read_errors: raise ValueError("file_unavailable")
            originals = _rows(raw[filename])
            by_title = {row["facts"]["scenario"]: row for row in originals}
            if len(by_title) != len(originals): raise ValueError("duplicate_scenario")
            configured = profiles[role]
            if {p["title"] for p in configured} != set(by_title): raise ValueError("profile_mismatch")
            normalized = []
            for profile in configured:
                row = by_title[profile["title"]]
                if any(not isinstance(row[key], str) for key in ("correct_analysis", "wrong_analysis", "error_reason")):
                    raise ValueError("invalid_legacy_case")
                requirements = dict(profile.get("data_requirements", {}))
                if profile.get("market_required"): requirements.update(dict.fromkeys(MARKET_FIELDS, True))
                # These are case patterns, not a second copy of the report JSON schema.
                fields = [key for key in ("short_term_view", "mid_term_view", "trend_state", "market_phase",
                          "fundamental_view", "event_view", "risk_level", "key_conflicts", "protective_conditions",
                          "invalidation_conditions", "max_confidence_allowed", "confidence_cap", "missing_evidence")
                          if key in SCHEMAS[role].model_fields]
                normalized.append(_case({**profile, "role": role, "market_context": {"scenario": profile["title"]},
                    "facts": row["facts"], "data_requirements": requirements,
                    "reasoning_pattern": row["correct_analysis"] + profile.get("focus", ""),
                    "expected_output_pattern": " / ".join(fields) + "；依据当前事实填写，缺失保留unavailable。",
                    "anti_patterns": [row["error_reason"]]}, role))
            normalized.extend(_case(row, role) for row in supplements if row["role"] == role)
            ids = [row.case_id for row in normalized]
            if len(ids) != len(set(ids)): raise ValueError("duplicate_case_id")
            cases[role] = tuple(sorted(normalized, key=lambda row: row.case_id))
        except (KeyError, ValueError, TypeError, UnicodeError): errors[role] = "case_parse_unavailable"
    return CaseLibrary(cases, errors, digest.hexdigest())


def score_details(case: ResearchCase, features: ScenarioFeatures) -> tuple[float, list[str]]:
    if case.role != features.role: return 0., []
    if not set(case.required_tags).issubset(features.tags): return 0., []
    if case.required_any and not set(case.required_any).intersection(features.tags): return 0., []
    if any(features.availability.get(k, False) != needed for k, needed in case.data_requirements.items()):
        return 0., []
    matched = sorted(set(case.scenario_tags) & features.tags)
    if not matched: return 0., []
    conflicts = sorted(set(case.signal_conflicts) & features.conflicts)
    evidence = sorted(set(case.evidence_patterns) & features.evidence_patterns)
    trend_match = features.trend_state in case.trend_states
    tag_w, data_w, trend_w, conflict_w, evidence_w = WEIGHTS[case.role]
    # Satisfying a gate counts as one relevance unit even if only one precise tag exists.
    score = 2 + tag_w * len(matched) + data_w * min(len(case.data_requirements), 3)
    score += trend_w * int(trend_match) + conflict_w * len(conflicts) + evidence_w * len(evidence)
    reasons = ["tag:" + tag for tag in matched]
    reasons += ["data:" + key + ("=available" if value else "=unavailable") for key, value in sorted(case.data_requirements.items())]
    reasons += ["conflict:" + name for name in conflicts] + ["evidence:" + name for name in evidence]
    if trend_match: reasons.append("trend:" + features.trend_state)
    return round(score, 3), reasons


def score_case(case: ResearchCase, scenario: ScenarioFeatures) -> float:
    return score_details(case, scenario)[0]


def estimate_tokens(text: str) -> int:
    # Provider-independent estimate, not billed tokens. CJK gets a conservative 1.5/token weight.
    non_ascii = sum(ord(char) > 127 for char in text)
    return math.ceil(non_ascii * 1.5 + (len(text) - non_ascii) / 4)


def _render_case(case: ResearchCase) -> str:
    facts = {key: value[:8] if isinstance(value, list) else str(value)[:120]
             for key, value in case.facts.items() if key in ("provided_fields", "missing_fields")}
    return json.dumps({"Case ID": case.case_id, "Scenario": case.title,
                       "Available Facts": facts, "Reasoning Pattern": case.reasoning_pattern[:220],
                       "Expected Structure": case.expected_output_pattern[:240],
                       "Key Warning": "；".join(case.anti_patterns)[:160]}, ensure_ascii=False, separators=(",", ":"))


HEADER = ("RELEVANT_CASES\n以下为推理模式示例，不是当前股票事实；不得复制案例观点或补入案例数据。"
          "当前FACT DATA、Schema、证据等级和confidence cap始终优先；缺失证据不得升级。\n"
          + "；".join(f"{key}: {value}" for key, value in RESEARCH_TIME_HORIZONS.items())
          + "。周期只是研究口径，不是预测承诺。\n")


def empty_audit(status: str, version: str | None, reason: str) -> dict:
    return {"few_shot_status": status, "few_shot_library_version": version,
            "selected_case_count": 0, "selected_case_ids": [], "retrieval_scores": [], "scores": [],
            "selection_reason": [reason], "few_shot_chars": 0, "estimated_few_shot_tokens": 0,
            "token_estimator": "cjk_1.5_ascii_0.25", "token_budget": TOKEN_BUDGET,
            "retriever_version": RETRIEVER_VERSION}


def select_cases(library: CaseLibrary, features: ScenarioFeatures, *, k: int | None = None,
                 minimum_score: float = MIN_SCORE, token_budget: int = TOKEN_BUDGET) -> tuple[str, dict]:
    role = features.role
    if role in library.errors or role not in library.cases:
        return "", empty_audit("unavailable", library.version, library.errors.get(role, "role_library_missing"))
    count = min(MAX_CASES, max(0, k if k is not None else (3 if features.conflicts else 2)))
    candidates = []
    for case in library.cases[role]:
        score, reasons = score_details(case, features)
        if score >= max(MIN_SCORE, minimum_score): candidates.append((case, score, reasons))
    selected, pieces, choices, fingerprints = [], [], [], set()
    budget = min(TOKEN_BUDGET, max(0, token_budget))
    while candidates and len(selected) < count:
        def adjusted(item):
            case, score, _ = item
            overlap = max((len(set(case.scenario_tags) & set(prev.scenario_tags)) /
                           len(set(case.scenario_tags) | set(prev.scenario_tags)) for prev in selected), default=0.)
            return round(score - overlap * 4, 3)
        candidates.sort(key=lambda item: (-adjusted(item), -item[1], item[0].case_id))
        item = candidates.pop(0)
        case, score, reasons = item
        effective_score = adjusted(item)
        fingerprint = (case.title, case.reasoning_pattern, case.expected_output_pattern)
        if case.case_id in {p.case_id for p in selected} or fingerprint in fingerprints: continue
        if effective_score < MIN_SCORE: continue
        rendered = _render_case(case)
        block = HEADER + "\n".join([*pieces, rendered])
        if estimate_tokens(block) > budget: continue  # Drop whole case; never cut JSON mid-field.
        selected.append(case); pieces.append(rendered); fingerprints.add(fingerprint)
        choices.append({"case_id": case.case_id, "score": score, "adjusted_score": effective_score,
                        "matched": reasons})
    if not selected:
        return "", empty_audit("no_match", library.version, "no_case_above_threshold_within_budget")
    block = HEADER + "\n".join(pieces)
    scores = [choice["score"] for choice in choices]
    return block, {**empty_audit("selected", library.version, "feature_match"),
                   "selected_case_count": len(selected), "selected_case_ids": [p.case_id for p in selected],
                   "retrieval_scores": scores, "scores": scores, "selection_reason": choices,
                   "few_shot_chars": len(block), "estimated_few_shot_tokens": estimate_tokens(block),
                   "token_budget": budget}


class FewShotRetriever:
    """One immutable library snapshot per research, shared by its five role selections."""
    def __init__(self, enabled: bool | None = None, root: Path | None = None):
        self.enabled = os.getenv("A_SHARE_FEW_SHOT_ENABLED", "0") == "1" if enabled is None else enabled
        self.library = None
        if self.enabled:
            try: self.library = load_library(root or DEFAULT_ROOT)
            except Exception: pass  # Optional local knowledge must never take down research.

    @property
    def version(self):
        return self.library.version if self.library else None

    def retrieve(self, role: str, facts: dict, specialists: dict | None = None) -> tuple[str, dict]:
        if not self.enabled: return "", empty_audit("disabled", None, "feature_flag_off")
        if self.library is None: return "", empty_audit("unavailable", None, "library_unavailable")
        try: return select_cases(self.library, scenario_features(role, facts, specialists))
        except Exception: return "", empty_audit("unavailable", self.version, "retrieval_unavailable")
