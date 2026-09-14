from datetime import datetime, timedelta

import pandas as pd

from src.market_cache import (SnapshotHistory, complete_daily_frame, daily_suspend_flag, infer_volume_unit,
                              normalize_instrument, realtime_ma, security_status, turnover_rate, validated_turnover)


def test_snapshot_speed_stays_unavailable_until_enough_time():
    history = SnapshotHistory()
    base = datetime(2026, 9, 11, 10, 0).timestamp()
    history.update("600000.SH", base, {"lastPrice": 10, "volume": 1, "amount": 1000})
    history.update("600000.SH", base + 59, {"lastPrice": 11, "volume": 2, "amount": 2100})
    assert history.speed("600000.SH", 1) == "unavailable"
    history.update("600000.SH", base + 60, {"lastPrice": 11, "volume": 2, "amount": 2100})
    assert history.speed("600000.SH", 1) == 10.0
    assert history.speed("600000.SH", 3) == "unavailable"


def test_volume_unit_requires_two_consistent_real_samples():
    instrument = {"float_volume": 1_000_000}
    ticks = [({"lastPrice": 10, "amount": 100_000, "volume": 100, "pvolume": 10_000}, instrument),
             ({"lastPrice": 20, "amount": 400_000, "volume": 200, "pvolume": 20_000}, instrument)]
    field, multiplier, evidence = infer_volume_unit(ticks)
    assert (field, multiplier) == ("pvolume", 1.0)
    assert len(evidence) == 2
    assert turnover_rate(ticks[0][0], instrument, field, multiplier) == 1.0


def test_turnover_requires_volume_and_pvolume_crosscheck():
    instrument = {"float_volume": 10_000_000}
    result = validated_turnover({"volume": 10_000, "pvolume": 1_000_000}, instrument)
    assert result["status"] == "verified"
    assert result["value"] == 10.0
    failed = validated_turnover({"volume": 10_000, "pvolume": 2_000_000}, instrument)
    assert failed["status"] == "turnover_validation_failed"
    assert failed["value"] == "unavailable"


def test_batch_turnover_keeps_each_stocks_tick_and_capital_mapping():
    cases = {
        "A": ({"volume": 100, "pvolume": 10_000}, {"float_volume": 10_000}),
        "B": ({"volume": 200, "pvolume": 20_000}, {"float_volume": 40_000}),
        "C": ({"volume": 500, "pvolume": 50_000}, {"float_volume": 20_000}),
    }
    results = {code: validated_turnover(tick, instrument)["value"]
               for code, (tick, instrument) in cases.items()}
    assert results == {"A": 100.0, "B": 50.0, "C": 250.0}
    assert len(set(results.values())) == 3


def test_three_stocks_keep_distinct_turnover_values():
    samples = [
        ({"volume": 100, "pvolume": 10_000}, {"float_volume": 10_000}),
        ({"volume": 200, "pvolume": 20_000}, {"float_volume": 40_000}),
        ({"volume": 500, "pvolume": 50_000}, {"float_volume": 20_000}),
    ]
    values = [validated_turnover(tick, instrument)["value"] for tick, instrument in samples]
    assert values == [100.0, 50.0, 250.0]
    assert len(set(values)) == 3


def test_three_stocks_keep_distinct_turnover_values():
    samples = {
        "A": ({"volume": 100, "pvolume": 10_000}, {"float_volume": 10_000}),
        "B": ({"volume": 200, "pvolume": 20_000}, {"float_volume": 40_000}),
        "C": ({"volume": 500, "pvolume": 50_000}, {"float_volume": 20_000}),
    }
    results = {code: validated_turnover(tick, instrument)["value"]
               for code, (tick, instrument) in samples.items()}
    assert results == {"A": 100.0, "B": 50.0, "C": 250.0}
    assert len(set(results.values())) == 3


def test_one_sample_does_not_enable_turnover():
    field, multiplier, _ = infer_volume_unit([({"lastPrice": 10, "amount": 100_000, "volume": 100}, {"float_volume": 1})])
    assert field is None and multiplier is None


def test_realtime_ma_uses_previous_completed_days_and_current_price():
    assert realtime_ma([1, 2, 3, 4], 5, 5) == 3.0
    assert realtime_ma([1, 2], 5, 5) == "unavailable"


def test_today_daily_bar_is_removed():
    now = datetime(2026, 9, 12, 10)
    frame = pd.DataFrame({"time": [int((now - timedelta(days=1)).timestamp() * 1000), int(now.timestamp() * 1000)],
                          "close": [10, 99]})
    complete = complete_daily_frame(frame, now)
    assert complete["close"].tolist() == [10]


def test_qmt_yyyymmdd_index_wins_over_utc_millisecond_date():
    frame = pd.DataFrame({"time": [1788969600000, 1789056000000], "close": [10, 99]}, index=[20260910, 20260911])
    complete = complete_daily_frame(frame, datetime(2026, 9, 11, 15, 0))
    assert complete["close"].tolist() == [10]


def test_history_after_stale_quote_date_is_not_used():
    frame = pd.DataFrame({"close": [10, 11, 99]}, index=[20260910, 20260911, 20260914])
    complete = complete_daily_frame(frame, datetime(2026, 9, 11, 15, 0))
    assert complete["close"].tolist() == [10]


def test_instrument_and_trading_state():
    instrument = normalize_instrument("600000.SH", {"InstrumentName": "浦发银行", "IsTrading": False})
    tick = {"lastPrice": 10, "lastClose": 9, "stockStatus": 5}
    assert instrument["name"] == "浦发银行"
    assert security_status(tick, 0) == "normal"


def test_corrupt_float_volume_is_rejected_and_status_suspends():
    instrument = normalize_instrument("600000.SH", {"FloatVolume": 3.1e-312, "InstrumentStatus": 1})
    assert instrument["float_volume"] is None
    assert security_status({"lastPrice": 10, "lastClose": 9}, 1) == "suspended"


def test_instrument_capital_requires_total_not_less_than_float():
    instrument = normalize_instrument("600000.SH", {"FloatVolume": 2_000_000, "TotalVolume": 1_000_000})
    assert instrument["float_volume"] is None
    assert instrument["total_volume"] is None


def test_suspend_flag_statuses_and_raw_tick_codes_are_not_invented():
    tick = {"lastPrice": 10, "lastClose": 9, "stockStatus": 987}
    assert security_status(tick, 0) == "normal"
    assert security_status(tick, -1) == "resumed_today"
    assert security_status(tick, "unavailable") == "unknown"


def test_daily_suspend_flag_uses_current_trading_day():
    frame = pd.DataFrame({"suspendFlag": [0, 1]}, index=[20260910, 20260911])
    assert daily_suspend_flag(frame, datetime(2026, 9, 11, 10)) == 1


def test_snapshot_speed_never_crosses_lunch_day_or_auction():
    history = SnapshotHistory()
    def stamp(day, hour, minute, second=0):
        return datetime(2026, 9, day, hour, minute, second).timestamp()
    tick = lambda price: {"lastPrice": price, "volume": 1, "amount": 1000}
    history.update("X", stamp(11, 11, 30), tick(10))
    history.update("X", stamp(11, 13, 0), tick(11))
    assert history.speed("X", 1) == "unavailable"
    history.update("X", stamp(11, 13, 1), tick(12))
    assert history.speed("X", 1) == round((12 / 11 - 1) * 100, 4)
    history.update("X", stamp(12, 9, 25), tick(20))  # ignored auction tick
    history.update("X", stamp(12, 9, 30), tick(20))
    assert history.speed("X", 5) == "unavailable"
