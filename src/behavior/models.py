"""Versioned research contracts. Outcomes never form model inputs."""
from dataclasses import asdict, dataclass
import hashlib
import json

VERSION = "p3-v1.1"
FEATURES = (
    "direction", "duration", "return", "max_drawdown", "max_runup",
    "atr", "volatility", "ma5", "ma10", "ma20", "ma60", "rsi", "k", "d", "j",
    "macd", "macd_histogram", "volume_ratio", "volume_trend", "amount_trend",
    "distance_to_ma20", "distance_to_ma60", "distance_to_20d_high",
    "distance_to_60d_high", "previous_swing_return", "previous_swing_duration",
)
HORIZONS = (1, 3, 5, 10, 20)

@dataclass(frozen=True)
class BehaviorConfig:
    atr_window: int = 14
    volatility_window: int = 20
    atr_multiple: float = 2.5
    volatility_multiple: float = 2.0
    min_bars: int = 3
    max_clusters: int = 5
    min_samples: int = 5
    normal_samples: int = 10
    similarity_floor: float = 0.35
    outcome_horizon: int = 5
    cost_buffer: float = 0.004
    min_oos_samples: int = 5
    def __post_init__(self):
        if min(self.atr_window, self.volatility_window, self.min_bars, self.max_clusters, self.min_samples) < 1:
            raise ValueError("positive_configuration_required")
        if self.normal_samples < self.min_samples or min(self.atr_multiple, self.volatility_multiple) <= 0:
            raise ValueError("invalid_thresholds")
        if not 0 < self.similarity_floor <= 1 or self.outcome_horizon not in HORIZONS:
            raise ValueError("invalid_pattern_configuration")
    @property
    def key(self):
        return hashlib.sha256((VERSION+json.dumps(asdict(self), sort_keys=True)).encode()).hexdigest()[:12]

@dataclass(frozen=True)
class Pivot:
    pivot_index: int
    confirmation_index: int
    pivot_time: str
    confirmation_time: str
    pivot_price: float
    direction: str

@dataclass(frozen=True)
class PatternStrategyCandidate:
    strategy_id: str
    version: str
    pattern_id: str
    entry_condition: dict
    confirmation: str
    stop_loss: float
    take_profit: float
    max_holding_days: int
    confidence: str
    data_end_date: str
    usable_from: str
    evidence: dict
