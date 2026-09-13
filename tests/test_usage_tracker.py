from src.usage_tracker import UsageRecord, UsageTracker


def test_usage_jsonl_and_summary_exclude_secrets(tmp_path):
    path = tmp_path / "usage.jsonl"
    tracker = UsageTracker(path)
    tracker.record(UsageRecord("A_1", "openai", "model", "risk_officer", 10, 5, 15, 0.2, 0.01,
                               "2026-09-13T10:00:00+08:00", True))
    row = tracker.records()[0]
    assert row["total_tokens"] == 15
    assert "api_key" not in row
    assert tracker.summary()["risk_officer"] == {"calls": 1, "total_tokens": 15, "estimated_cost": 0.01}
