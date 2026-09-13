import threading
import time

from src.agent import StockResearchAgent
from src.llm_router import RouterResult


class FakeTracker:
    def summary(self):
        return {}


class FakeRouter:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.calls = []
        self.lock = threading.Lock()
        self.tracker = FakeTracker()
        self.last_results = {}

    def call(self, role, messages, schema, analysis_id, analysis_mode="standard"):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(role)
        if role != "chief_researcher":
            time.sleep(0.04)
        with self.lock:
            self.active -= 1
        data = {"summary": role} if role != "chief_researcher" else {"final_summary": "done"}
        return RouterResult(role, "mock", "mock-model", True, data, 0.04)


def test_four_employees_run_in_parallel_then_chief_and_share_analysis_id():
    router = FakeRouter()
    facts = {"lastPrice": 10, "missing": "unavailable"}
    result = StockResearchAgent(router).analyze("600000.SH", facts)
    assert router.max_active == 4
    assert router.calls[-1] == "chief_researcher"
    assert set(result["employees"]) == {"technical_analyst", "fundamental_event_analyst",
                                        "sentiment_analyst", "risk_officer"}
    assert result["analysis_id"].startswith("600000.SH_")
    assert result["analysis_id"].endswith("_standard")
    assert result["fact_data"] == facts
    assert all(row["start_time"] and row["end_time"] for row in result["employees"].values())


def test_failed_employee_is_unavailable_but_chief_still_runs():
    class PartlyFailedRouter(FakeRouter):
        def call(self, role, messages, schema, analysis_id, analysis_mode="standard"):
            if role == "sentiment_analyst":
                self.calls.append(role)
                return RouterResult(role, None, None, False, None, 0.0, error="all providers failed")
            return super().call(role, messages, schema, analysis_id, analysis_mode)

    router = PartlyFailedRouter()
    result = StockResearchAgent(router).analyze("600000.SH", {"lastPrice": 10})
    assert not result["employees"]["sentiment_analyst"]["success"]
    assert result["chief_researcher"]["success"]
    assert router.calls[-1] == "chief_researcher"


def test_all_specialists_fail_blocks_chief():
    class FailedRouter(FakeRouter):
        def call(self, role, messages, schema, analysis_id, analysis_mode="standard"):
            self.calls.append(role)
            return RouterResult(role, None, None, False, None, 0.0, error="provider failed")

    router = FailedRouter()
    result = StockResearchAgent(router).analyze("600000.SH", {"last_price": 10})
    assert router.calls.count("chief_researcher") == 0
    assert result["chief_researcher"]["status"] == "insufficient_specialist_results"


def test_one_specialist_success_runs_degraded_chief_without_error_text():
    class OneSuccessRouter(FakeRouter):
        def call(self, role, messages, schema, analysis_id, analysis_mode="standard"):
            if role == "technical_analyst" or role == "chief_researcher":
                return super().call(role, messages, schema, analysis_id, analysis_mode)
            self.calls.append(role)
            return RouterResult(role, None, None, False, None, 0.0, error="SECRET_PROVIDER_ERROR")

    router = OneSuccessRouter()
    result = StockResearchAgent(router).analyze("600000.SH", {"last_price": 10})
    assert result["chief_researcher"]["success"]
    assert result["chief_researcher"]["status"] == "degraded"
    assert "SECRET_PROVIDER_ERROR" not in str(router.calls)


def test_role_fact_bundles_make_missing_domains_explicit():
    facts = {"code": "600498.SH", "last_price": 10, "fundamental_data": "unavailable",
             "event_data": "unavailable", "sentiment_external_data": "unavailable"}
    fundamental = StockResearchAgent._role_facts("fundamental_event_analyst", facts)
    sentiment = StockResearchAgent._role_facts("sentiment_analyst", facts)
    assert fundamental["role_specific_data"]["fundamental_data"] == "unavailable"
    assert fundamental["role_specific_data"]["event_data"] == "unavailable"
    assert sentiment["role_specific_data"]["sentiment_external_data"] == "unavailable"
