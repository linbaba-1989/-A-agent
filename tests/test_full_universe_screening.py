"""Regression coverage for screening the universe before display truncation."""
import math

import pytest

from src.scanner import MarketScanner
from test_scanner import FakeProvider
from ui.view_models import filter_scan_rows


class LargeUniverseProvider(FakeProvider):
    def get_stock_universe(self):
        return [f'{index:06d}.SZ' for index in range(1, 151)]

    def get_full_ticks(self, symbols, batch_size=500):
        ticks = super().get_full_ticks(symbols, batch_size)
        # The only qualifying stock ranks below the old 100-row cutoff.
        ticks['000150.SZ']['lastPrice'] = 10.4
        return ticks


def test_full_scan_finds_candidate_below_previous_top_100_cutoff():
    scanner = MarketScanner(LargeUniverseProvider())
    limited = scanner.scan(100)
    complete = scanner.scan(None)
    filters = {'change_min': 3, 'change_max': 5}
    assert len(complete.rows) == 150
    assert filter_scan_rows(limited.rows, filters) == []
    assert [row['symbol'] for row in filter_scan_rows(complete.rows, filters)] == ['000150.SZ']


@pytest.mark.parametrize('value', [None, 'unavailable', math.nan, math.inf, -math.inf])
@pytest.mark.parametrize('filters', [
    {'change_min': 3, 'change_max': 5},
    {'above_ma5': True},
    {'recent_high': True},
    {'ma_breakout': True},
])
def test_missing_or_nonfinite_required_values_never_pass(value, filters):
    row = {'symbol': '600498.SH', 'lastPrice': value, 'change_pct': value,
           'ma5': 10, 'high_20d': 10, 'lastClose': 9, 'previous_ma5': 10}
    assert filter_scan_rows([row], filters) == []


def test_inclusive_change_boundaries_and_all_conditions_required():
    rows = [{'symbol': f'{index:06d}.SZ', 'lastPrice': 11, 'ma5': ma, 'change_pct': change}
            for index, (change, ma) in enumerate([(3, 10), (5, 10), (4, 12), (2.99, 10)], 1)]
    assert [row['symbol'] for row in filter_scan_rows(rows, {
        'change_min': 3, 'change_max': 5, 'above_ma5': True})] == ['000001.SZ', '000002.SZ']
