"""Frozen P4 research contracts and explicit availability times."""
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import numpy as np
import pandas as pd

VERSION = 'p4-v1.0'
INDICES = ('000001.SH', '399001.SZ', '399006.SZ')
REGIMES = ('SHORT_HOT', 'SHORT_REPAIR', 'TREND_STRONG', 'TREND_ROTATION', 'MIXED', 'RISK_OFF', 'UNKNOWN')
BREADTH_FIELDS = frozenset('''expected_symbols observed_symbols return_samples ma5_samples ma20_samples ma60_samples
advances declines unchanged gain_gt3 gain_gt5 loss_lt3 loss_lt5 above_ma5 above_ma20 above_ma60 new_high20 new_low20
median_return dispersion total_amount volume_up_ratio volume_down_ratio momentum_persistence1 momentum_persistence3
active_large_move_ratio high_volatility_participation large_move_amount_share top_decile_amount_share coverage return_coverage indicator_coverage
advance_ratio decline_ratio amount_change amount_ratio20 breadth_persistence5 new_low_expansion intraday_samples
market_30m_acceleration market_30m_up_ratio market_30m_dispersion'''.split())
INDEX_FIELDS = frozenset('''close return_1 return_3 return_5 return_20 ma5 ma10 ma20 ma60 ma20_slope rsi macd
macd_histogram atr atr_ratio realized_volatility drawdown_20 drawdown_60 trend_persistence above_ma20 above_ma60
trend_30m macd_30m volatility_30m acceleration_30m'''.split())

def validate_features(record):
    if set(record['breadth'])-BREADTH_FIELDS or any(set(v)-INDEX_FIELDS for v in record['index_state'].values()):
        raise ValueError('regime_feature_allowlist_violation')

@dataclass(frozen=True)
class RegimeConfig:
    min_coverage: float = .80
    min_indicator_coverage: float = .70
    min_independent_days: int = 10
    risk_off: float = 65
    strong_trend: float = 68
    hot_short: float = 68
    frequency: str = '1d'

    def __post_init__(self):
        if not 0<self.min_coverage<=1 or not 0<self.min_indicator_coverage<=1:
            raise ValueError('invalid_coverage_threshold')
        if self.min_independent_days<10 or self.frequency!='1d':
            raise ValueError('unsupported_evidence_or_frequency')
        if any(not 0<x<100 for x in (self.risk_off,self.strong_trend,self.hot_short)):
            raise ValueError('invalid_score_threshold')

    @property
    def key(self):
        return hashlib.sha256((VERSION+json.dumps(asdict(self), sort_keys=True)).encode()).hexdigest()[:12]

def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value=value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value

def encode(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False, default=str)

def available_at(day):
    # One explicit processing minute after the last required close.
    return pd.Timestamp(day).normalize()+pd.Timedelta(hours=15, minutes=1)
