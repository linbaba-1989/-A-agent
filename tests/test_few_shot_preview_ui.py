"""The visible case preview is local and cannot leak cases across roles."""

from src.few_shot import FewShotRetriever, load_library
from src.few_shot_features import ROLES
from src.llm_router import LLMRouter
from ui.few_shot_preview import case_details_for_audit, preview_cases


def test_preview_retrieves_five_roles_while_production_stays_off(monkeypatch):
    monkeypatch.delenv("A_SHARE_FEW_SHOT_ENABLED", raising=False)
    facts = {"last_price": 10, "volume_ratio": 2, "high_20d": 9}
    original = dict(facts)

    preview = preview_cases(facts)

    assert not FewShotRetriever().enabled
    assert facts == original
    assert set(preview) == set(ROLES)
    assert all(row["count"] == len(row["cases"]) for row in preview.values())
    assert all(row["status"] in {"selected", "no_match", "unavailable"} for row in preview.values())
    assert any(row["count"] for row in preview.values())


def test_preview_case_count_and_role_isolation():
    library = load_library()
    assert not library.errors
    facts = {"last_price": 10, "volume_ratio": 2, "high_20d": 9}
    preview = preview_cases(facts)
    retriever = FewShotRetriever(enabled=True)
    for role, row in preview.items():
        _, audit = retriever.retrieve(role, facts)
        own_ids = {case.case_id for case in library.cases[role]}
        assert row["count"] == len(row["cases"])
        assert row["count"] == audit["selected_case_count"]
        assert [case["case_id"] for case in row["cases"]] == audit["selected_case_ids"]
        assert row["count"] <= 3
        assert all(set(case) == {"case_id", "title", "score"} for case in row["cases"])
        assert all(case["case_id"] in own_ids for case in row["cases"])

    wrong_role_audit = {
        "technical_analyst": {
            "selected_case_ids": ["fundamental-08", "technical-01"],
            "retrieval_scores": [8.4, 7.9],
        }
    }
    details = case_details_for_audit(wrong_role_audit)
    assert [case["case_id"] for case in details["technical_analyst"]] == ["technical-01"]
    assert details["technical_analyst"][0]["score"] == 7.9
    assert details["fundamental_event_analyst"] == []


def test_preview_never_calls_a_model(monkeypatch):
    def forbidden_model_call(*args, **kwargs):
        raise AssertionError("preview must not call an AI model")

    monkeypatch.setattr(LLMRouter, "call", forbidden_model_call)
    preview = preview_cases({"last_price": 10, "volume_ratio": 2, "high_20d": 9})
    assert preview["technical_analyst"]["count"] >= 1
