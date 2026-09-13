import json

import pytest

from qmt_bridge import qmt_internal_market_bridge as bridge
from scripts.qmt_bridge_probe import BridgeSnapshotError, read_snapshot, times_advanced


CODES = ("600498.SH", "600000.SH", "000001.SZ")


def payload(timestamp=1_700_000_000_000):
    fields = {"time": timestamp, "lastPrice": 10.5, "lastClose": 10.0, "open": 10.1,
              "high": 10.8, "low": 9.9, "volume": 100, "pvolume": 10000,
              "amount": 105000, "stockStatus": 3}
    return {"schema_version": 1, "source": "QMT_INTERNAL_PYTHON", "written_at_ms": timestamp,
            "ticks": {code: {"code": code, **fields} for code in CODES}}


def test_reads_complete_three_stock_snapshot(tmp_path):
    path = tmp_path / "latest_ticks.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")
    result = read_snapshot(path)
    assert set(result["ticks"]) == set(CODES)


def test_time_advancement_requires_new_tick_time():
    before = payload()
    unchanged = payload()
    advanced = payload()
    advanced["ticks"]["600498.SH"]["time"] += 1000
    assert not times_advanced(before, unchanged)
    assert times_advanced(before, advanced)


@pytest.mark.parametrize("content", ["{half", "[]", '{"source":"wrong","ticks":{}}'])
def test_rejects_partial_or_invalid_files(tmp_path, content):
    path = tmp_path / "latest_ticks.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BridgeSnapshotError):
        read_snapshot(path)


def test_atomic_replace_keeps_reader_on_complete_document(tmp_path):
    target = tmp_path / "latest_ticks.json"
    temporary = tmp_path / "latest_ticks.json.tmp"
    target.write_text(json.dumps(payload()), encoding="utf-8")
    newer = payload(1_700_000_001_000)
    temporary.write_text(json.dumps(newer), encoding="utf-8")
    temporary.replace(target)
    assert read_snapshot(target)["written_at_ms"] == 1_700_000_001_000


def test_internal_bridge_subscribes_and_atomically_flushes(tmp_path, monkeypatch):
    output = tmp_path / "latest_ticks.json"
    monkeypatch.setattr(bridge, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "OUTPUT_FILE", str(output))
    monkeypatch.setattr(bridge, "TEMP_FILE", str(tmp_path / "latest_ticks.json.tmp"))
    bridge.LATEST_TICKS.clear()

    class Context:
        def subscribe_whole_quote(self, codes, callback=None):
            self.codes, self.callback = codes, callback
            return 7

        def run_time(self, *args):
            self.timer = args

    context = Context()
    bridge.init(context)
    context.callback(payload()["ticks"])
    bridge.flush(context)
    result = read_snapshot(output)
    assert context.codes == list(CODES)
    assert context.timer == ("flush", "1nSecond", "2020-01-01 00:00:00")
    assert result["subscription_id"] == 7
    assert not (tmp_path / "latest_ticks.json.tmp").exists()
