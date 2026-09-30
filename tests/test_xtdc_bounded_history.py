"""Bounded opt-in XTDC history calls preserve the legacy production interface."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.xtdc_provider import XtDataCenterProvider


class Backend:
    def __init__(self):
        self.calls = []

    def request(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == "download_history_data2":
            return None
        return {"600498.SH": {"columns": ["time", "close"],
                             "index": ["20260930"], "data": [[1790726400000, 35.7]]}}


@pytest.mark.parametrize("adjustment,sdk", [("raw", "none"), ("qfq", "front"), ("hfq", "back")])
def test_explicit_range_and_adjustment_provenance(adjustment, sdk):
    provider = XtDataCenterProvider(token="")
    provider.backend = Backend()
    frame = provider.get_history_range(["600498.SH"], "30m", "20260901", "20260930",
                                       adjustment=adjustment)["600498.SH"]
    command, args = provider.backend.calls[0]
    assert command == "get_market_data_ex"
    assert (args["start_time"], args["end_time"], args["dividend_type"]) == ("20260901", "20260930", sdk)
    assert frame.attrs["source"] == "xtdc" and frame.attrs["adjustment"] == adjustment
    assert frame.attrs["provider"] == "XtDataCenter Token"
    assert frame.iloc[0]["source"] == "xtdc"
    assert frame.iloc[0]["adjustment"] == adjustment
    assert frame.iloc[0]["status"] == "HISTORICAL"
    assert frame.iloc[0]["trade_date"] == "2026-09-30"
    assert frame.iloc[0]["quote_time"].endswith("+08:00")
    assert frame.attrs["base_period"] == "5m"


@pytest.mark.parametrize("period", ["15m", "30m"])
def test_derived_period_downloads_base_bars_and_reads_requested_period(period):
    provider = XtDataCenterProvider(token="")
    provider.backend = Backend()
    provider.download_history_range(["600498.SH"], period, "20260901", "20260930")
    provider.get_history_range(["600498.SH"], period, "20260901", "20260930")
    assert provider.backend.calls[0][1]["period"] == "5m"
    assert provider.backend.calls[1][1]["period"] == period


@pytest.mark.parametrize("start,end", [("", "20260930"), ("20260901", ""),
    ("20260930", "20260901"), ("20240101", "20260930"), ("20260230", "20260930")])
def test_invalid_or_unbounded_ranges_never_request(start, end):
    provider = XtDataCenterProvider(token="")
    provider.backend = Backend()
    with pytest.raises(ValueError):
        provider.download_history_range(["600498.SH"], "1m", start, end)
    assert provider.backend.calls == []


def test_download_deduplicates_and_rejects_oversized_batch():
    provider = XtDataCenterProvider(token="")
    provider.backend = Backend()
    provider.download_history_range(["600498.SH"] * 2, "1m", "20260901000000", "20260930235959")
    assert provider.backend.calls[0][1]["symbols"] == ["600498.SH"]
    with pytest.raises(ValueError):
        provider.download_history_range([f"{i:06d}.SZ" for i in range(51)], "1m", "20260901", "20260930")
    assert len(provider.backend.calls) == 1


def test_empty_upstream_frame_stays_unavailable_without_synthetic_bars():
    provider = XtDataCenterProvider(token="")
    provider.backend = SimpleNamespace(request=lambda *a, **k:
        {"600498.SH": {"columns": [], "index": [], "data": []}})
    frame = provider.get_history_range(["600498.SH"], "1m", "20260901", "20260930")["600498.SH"]
    assert frame.empty and frame.attrs["status"] == "UNAVAILABLE"
    assert {"source", "provider", "trade_date", "quote_time", "status", "adjustment"} <= set(frame.columns)


@pytest.mark.parametrize("period,adjustment", [("tick", "raw"), ("1m", "unknown")])
def test_unsupported_history_contract_never_requests(period, adjustment):
    provider = XtDataCenterProvider(token="")
    provider.backend = Backend()
    with pytest.raises(ValueError):
        provider.get_history_range(["600498.SH"], period, "20260901", "20260930", adjustment=adjustment)
    assert provider.backend.calls == []


def test_worker_passes_bounds_and_disables_fill(monkeypatch):
    import pandas as pd
    worker_path = Path(__file__).resolve().parents[1] / "scripts/xtdc_worker.py"
    spec = importlib.util.spec_from_file_location("bounded_test_worker", worker_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    calls = []
    def read(**kwargs):
        calls.append(kwargs)
        return {"600498.SH": pd.DataFrame({"time": [1], "close": [35.7]})}
    fake = SimpleNamespace(xtdatacenter=SimpleNamespace(set_token=lambda _: None, init=lambda: None,
        shutdown=lambda: None), xtdata=SimpleNamespace(get_market_data_ex=read, __file__="test"))
    fake.xtdatacenter.__file__ = "test"
    monkeypatch.setitem(__import__("sys").modules, "xtquant", fake)
    requests = [dict(id=1, command="init", token="test-only"),
        dict(id=2, command="get_market_data_ex", fields=["time", "close"], symbols=["600498.SH"],
             period="1m", start_time="20260901", end_time="20260930", dividend_type="front"),
        dict(id=3, command="close")]
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO("\n".join(json.dumps(x) for x in requests)))
    output = io.StringIO()
    monkeypatch.setattr(worker.sys, "stdout", output)
    monkeypatch.setattr(worker.sys, "path", list(worker.sys.path))
    worker.main("test-runtime")
    assert all(x["success"] for x in map(json.loads, output.getvalue().splitlines()))
    assert calls[0]["fill_data"] is False
    assert calls[0]["start_time"] == "20260901" and calls[0]["end_time"] == "20260930"
    assert calls[0]["dividend_type"] == "front"
