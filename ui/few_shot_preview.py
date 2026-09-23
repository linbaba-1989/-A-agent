"""Read-only case summaries for the AI research workbench.

The preview intentionally runs local retrieval even when production Few-shot is
off. It never builds a prompt, creates an agent, or calls an AI model.
"""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from src.few_shot import CaseLibrary, FewShotRetriever, load_library
from src.few_shot_features import ROLES


def _case_summaries(role: str, audit: Mapping[str, Any], library: CaseLibrary | None) -> list[dict]:
    """Show only cases from this role; never expose the case prompt/body."""
    if library is None:
        return []
    own_cases = {case.case_id: case for case in library.cases.get(role, ())}
    case_ids = audit.get("selected_case_ids", [])
    scores = audit.get("retrieval_scores", [])
    if not isinstance(case_ids, list):
        return []
    if not isinstance(scores, list):
        scores = []

    summaries = []
    for index, case_id in enumerate(case_ids):
        case = own_cases.get(case_id) if isinstance(case_id, str) else None
        if case is None:
            continue
        score = scores[index] if index < len(scores) else None
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score):
            score = None
        summaries.append({"case_id": case.case_id, "title": case.title, "score": score})
    return summaries


def preview_cases(facts: dict) -> dict[str, dict]:
    """Preview all five role selections from the current Fact Bundle, locally.

    This calls the same retriever used in research with an explicit preview-only
    enablement. The caller's production setting and Fact Bundle are unchanged.
    """
    retriever = FewShotRetriever(enabled=True)
    previews = {}
    for role in ROLES:
        _, audit = retriever.retrieve(role, facts)
        cases = _case_summaries(role, audit, retriever.library)
        previews[role] = {
            "status": audit["few_shot_status"],
            "count": len(cases),
            "cases": cases,
            "library_version": audit["few_shot_library_version"],
        }
    return previews


def case_details_for_audit(audit: dict) -> dict[str, list[dict]]:
    """Resolve saved research case IDs to compact, role-isolated labels."""
    library = load_library()
    return {
        role: _case_summaries(role, audit.get(role, {}) if isinstance(audit.get(role), dict) else {}, library)
        for role in ROLES
    }
