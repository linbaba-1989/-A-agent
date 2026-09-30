"""Exercise the real Scanner page with deterministic market boundaries."""
import io
import math
from types import SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.models import MarketDiagnostics, ScanResult
from ui.view_models import filter_scan_rows


class ScannerStub:
    def __init__(self, rows=None, connected=True, message="", history_status="ready"):
        self.calls = []
        self.rows = rows or []
        self.connected, self.message = connected, message
        self.history_service = SimpleNamespace(info=lambda: SimpleNamespace(
            status=history_status, ready=0, total=len(self.rows),
            insufficient=0, unavailable=len(self.rows), failed=0))

    def scan(self, top_n=30, progress_callback=None):
        self.calls.append(top_n)
        return ScanResult(self.rows, MarketDiagnostics(
            self.connected, None, message=self.message, elapsed_seconds=.01))


def page_script():
    import streamlit as st
    from ui.pages.scanner import render
    render({"scanner": st.session_state["test_scanner"], "available": True,
            "status": {"market_session": "closed", "active_universe": 150}})


def page(scanner):
    app = AppTest.from_function(page_script)
    app.session_state["test_scanner"] = scanner
    return app.run()


def scan(app):
    next(button for button in app.button if button.label == "开始扫描").click().run()
    assert not app.exception


@pytest.mark.parametrize("prefix", ["price", "change", "turnover"])
def test_invalid_range_does_not_start_scan(prefix):
    scanner = ScannerStub()
    app = page(scanner)
    app.number_input(key=prefix + "_min").set_value(5)
    app.number_input(key=prefix + "_max").set_value(3)
    scan(app)
    assert scanner.calls == []
    assert "最低值不能大于最高值" in app.error[0].value


def test_full_filtered_results_display_100_and_export_all_without_rescan(monkeypatch):
    from ui.pages import scanner as module
    exports = []
    original = module.st.download_button

    def capture(label, data, **kwargs):
        exports.append(data)
        return original(label, data, **kwargs)

    monkeypatch.setattr(module.st, "download_button", capture)
    rows = [dict(symbol=f"{i:06d}.SZ", name="测试股票", lastPrice=11, change_pct=4,
                 amount=1000000, turnover_rate=2, ma5=10, speed_1m=.5,
                 quote_time="2026-09-30T15:00:00+08:00", source="test",
                 trade_date="2026-09-30", field_provenance={"price": "test"})
            for i in range(150)]
    rows[0]["change_pct"] = 6
    scanner = ScannerStub(rows)
    app = page(scanner)
    app.number_input(key="change_min").set_value(3)
    app.number_input(key="change_max").set_value(5)
    scan(app)
    assert scanner.calls == [None]
    assert len(app.session_state["scan_rows"]) == 150
    assert len(app.dataframe[0].value) == 100
    assert any("扫描返回：150｜当前筛选：149" in x.value for x in app.caption)
    assert exports[-1].startswith(b"\xef\xbb\xbf")
    exported = pd.read_csv(io.BytesIO(exports[-1]), encoding="utf-8-sig")
    assert len(exported) == 149
    assert "field_provenance" not in exported.columns
    assert set(rows[1]) - {"field_provenance"} == set(exported.columns)
    assert exported["symbol"].tolist() == [row["symbol"] for row in rows[1:]]
    assert exported["name"].iloc[0] == "测试股票"
    app.number_input(key="change_min").set_value(4).run()
    assert not app.exception and scanner.calls == [None]


@pytest.mark.parametrize("status", ["not_started", "initializing", "failed", "partial"])
def test_history_not_ready_does_not_disable_requested_ma(status):
    scanner = ScannerStub([dict(symbol="000001.SZ", lastPrice=11)], history_status=status)
    app = page(scanner)
    next(box for box in app.checkbox if box.label == "MA5").check()
    scan(app)
    assert any("当前筛选：0" in x.value for x in app.caption)
    assert next(box for box in app.checkbox if box.label == "MA5").value


@pytest.mark.parametrize("message", ["行情网络断开", ""])
def test_disconnected_scan_clears_previous_results(message):
    scanner = ScannerStub(connected=False, message=message)
    app = page(scanner)
    app.session_state["scan_rows"] = [dict(symbol="000001.SZ", lastPrice=11)]
    app.session_state["last_scan_elapsed"] = 10
    scan(app)
    assert scanner.calls == [None]
    assert app.session_state["scan_rows"] == []
    assert app.error[0].value == (message or "行情连接失败，本次扫描未取得有效结果")
    assert not app.dataframe
    assert not app.get("download_button")
    app.run()
    assert any("扫描返回：0｜当前筛选：0" in x.value for x in app.caption)


@pytest.mark.parametrize("value", [None, "unavailable", math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field,filters", [
    ("lastPrice", {"price_min": 1}),
    ("change_pct", {"change_max": 5}),
    ("turnover_rate", {"turnover_min": 1}),
    ("amount", {"amount_min": 1}),
    ("speed_1m", {"speed_1m": .1}),
    ("speed_3m", {"speed_3m": .1}),
    ("speed_5m", {"speed_5m": .1}),
    ("ma5", {"above_ma5": True}),
    ("ma10", {"above_ma10": True}),
    ("ma20", {"above_ma20": True}),
    ("ma60", {"above_ma60": True}),
    ("high_20d", {"recent_high": True}),
    ("previous_ma5", {"ma_breakout": True}),
])
def test_each_required_field_rejects_missing_and_nonfinite_values(field, filters, value):
    row = dict(symbol="000001.SZ", lastPrice=11, lastClose=9, change_pct=4,
               turnover_rate=2, amount=100, speed_1m=.5, speed_3m=.5, speed_5m=.5,
               ma5=10, ma10=10, ma20=10, ma60=10, high_20d=10, previous_ma5=10)
    assert filter_scan_rows([row], filters) == [row]
    row[field] = value
    assert filter_scan_rows([row], filters) == []
