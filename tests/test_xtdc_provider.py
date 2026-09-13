import pandas as pd

from src.xtdc_provider import XtDataCenterProvider


class FakeBackend:
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False

    def start(self, token):
        if self.fail:
            raise RuntimeError("init failed token=" + token)
        return {"version": "test", "xtdata_path": "token/xtquant/xtdata.py"}

    def request(self, command, **kwargs):
        if command == "get_sector_list":
            return ["沪深A股"]
        if command == "get_stock_list_in_sector":
            return ["600000.SH", "000001.SZ", "430001.BJ"]
        if command == "get_quote_server_status":
            return {"0_SH_L1": "ok", "0_SZ_L1": "ok", "0_BJ_L1": "ok"}
        if command == "get_full_tick":
            return {code: {"time": 1700000000000, "lastPrice": 10, "lastClose": 9,
                           "volume": 2, "pvolume": 200, "amount": 2000} for code in kwargs["symbols"]}
        if command == "get_instrument_detail":
            return {"InstrumentName": "X", "ExchangeID": kwargs["symbol"].split(".")[-1],
                    "FloatVolume": 10000000, "TotalVolume": 20000000}
        if command == "get_local_data":
            return {code: {"columns": ["close"], "index": [1, 2], "data": [[9], [10]]}
                    for code in kwargs["symbols"]}
        raise AssertionError(command)

    def close(self):
        self.closed = True


def test_token_missing_does_not_start_backend():
    provider = XtDataCenterProvider(token="", backend_factory=lambda: FakeBackend())
    assert not provider.init()
    assert provider.init_error == "xtdc_token_missing"


def test_init_ticks_history_and_capital_are_compatible():
    provider = XtDataCenterProvider(token="secret", backend_factory=lambda: FakeBackend())
    assert provider.init()
    assert provider.check_connection().ok
    assert provider.get_stock_universe() == ["000001.SZ", "430001.BJ", "600000.SH"]
    assert provider.get_full_ticks(["600000.SH"])["600000.SH"]["lastPrice"] == 10
    history = provider.get_history(["600000.SH"], "1d", 2)
    assert isinstance(history["600000.SH"], pd.DataFrame)
    assert provider.normalized_instrument("600000.SH")["float_volume"] == 10000000
    audit = provider.audit_a_share_universe()
    assert audit["raw_count"] == audit["active_count"] == 3
    assert audit["missing_tick_count"] == 0


def test_init_failure_does_not_leak_token():
    provider = XtDataCenterProvider(token="top-secret", backend_factory=lambda: FakeBackend(True))
    assert not provider.init()
    assert "top-secret" not in provider.init_error
