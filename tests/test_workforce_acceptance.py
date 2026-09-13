from datetime import datetime

from src.workforce_acceptance import _market_state, _parallel_check, unsupported_numeric_claims


def test_closed_snapshot_and_numeric_claim_detection():
    timestamp = datetime(2026, 9, 11, 15, 0).timestamp()
    assert _market_state(timestamp, datetime(2026, 9, 13, 10, 0).astimezone()) == (
        "closed", "latest_available_snapshot")
    facts = {"last_price": 10.5, "ma5": 10.2, "speed_1m": "unavailable"}
    report = {"summary": "最新价10.5，未经支持的目标价12.34", "confidence": 80}
    claims = unsupported_numeric_claims(report, facts)
    assert [claim["value"] for claim in claims] == ["12.34"]


def test_parallel_check_requires_real_shared_overlap():
    overlapping = {
        "a": {"start_time": "2026-09-13T10:00:00.000+08:00", "end_time": "2026-09-13T10:00:03.000+08:00"},
        "b": {"start_time": "2026-09-13T10:00:01.000+08:00", "end_time": "2026-09-13T10:00:04.000+08:00"},
    }
    sequential = {
        "a": {"start_time": "2026-09-13T10:00:00.000+08:00", "end_time": "2026-09-13T10:00:01.000+08:00"},
        "b": {"start_time": "2026-09-13T10:00:02.000+08:00", "end_time": "2026-09-13T10:00:03.000+08:00"},
    }
    assert _parallel_check(overlapping)["pass"]
    assert not _parallel_check(sequential)["pass"]
