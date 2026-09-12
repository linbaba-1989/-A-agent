import sys
from types import ModuleType

import pytest

from src.qmt_provider import QMTProvider


class FakeXtData:
    def __init__(self):
        self.calls = []

    def get_full_tick(self, symbols):
        self.calls.append(symbols)
        return {symbol: {"lastPrice": 10, "lastClose": 9, "time": 1_700_000_000_000} for symbol in symbols}


def test_full_tick_is_batched_and_normalized():
    provider = QMTProvider()
    fake = FakeXtData()
    provider._xtdata = fake
    ticks = provider.get_full_ticks(["000001.SZ", "600000.SH", "000002.SZ"], batch_size=2)
    assert len(fake.calls) == 2
    assert len(ticks) == 3
    quote = provider.normalize_tick("000001.SZ", ticks["000001.SZ"])
    assert quote["source"] == "QMT/xtquant.get_full_tick"
    assert quote["timestamp"] != "unavailable"


def test_stock_code_filter_excludes_indices_and_funds():
    assert QMTProvider._is_a_share("600000.SH")
    assert QMTProvider._is_a_share("300001.SZ")
    assert not QMTProvider._is_a_share("000001.SH")
    assert not QMTProvider._is_a_share("510300.SH")


def test_invalid_tick_is_rejected():
    assert not QMTProvider._valid_tick({"lastPrice": 0, "lastClose": 10})
    assert not QMTProvider._valid_tick({})


def test_preloaded_external_xtquant_stops_instead_of_falling_back(monkeypatch):
    external = ModuleType("xtquant")
    external.__file__ = "C:/external/xtquant/__init__.py"
    monkeypatch.setitem(sys.modules, "xtquant", external)
    with pytest.raises(RuntimeError, match="xtquant_component_mixed"):
        QMTProvider()._load_xtdata()
