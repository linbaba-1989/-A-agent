import pandas as pd

from src.indicators import UNAVAILABLE, intraday_position, moving_average, percent_change, rolling_high, speed, vwap


def test_indicators_from_qmt_frames():
    daily = pd.DataFrame({"close": range(1, 21), "high": range(2, 22)})
    minute = pd.DataFrame({"close": [9.8, 9.9, 10.0], "amount": [980, 990, 1000], "volume": [1, 1, 1]})
    assert moving_average(daily, 5) == 18.0
    assert rolling_high(daily, 5) == 21.0
    assert speed(minute, 3, 10.1) == percent_change(10.1, 9.8)
    assert vwap(minute) == 9.9
    assert intraday_position(10, 8, 12) == 50.0


def test_missing_indicator_data_is_explicit():
    assert moving_average(pd.DataFrame(), 5) == UNAVAILABLE
    assert percent_change(10, 0) == UNAVAILABLE
    assert intraday_position(10, 10, 10) == UNAVAILABLE
