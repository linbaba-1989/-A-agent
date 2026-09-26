"""Canonical values: shares, CNY, percentages (3 means 3%), aware source time."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
import re
import math


def canonical_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("canonical_symbol_required")
    return symbol


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    name: str | None = None
    price: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    change: float | None = None
    pct_change: float | None = None
    volume_shares: float | None = None
    amount_cny: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    total_market_cap: float | None = None
    float_market_cap: float | None = None
    quote_time: datetime | None = None
    source: str = ""
    quote_status: str = "UNAVAILABLE"

    def __post_init__(self):
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        for name in ("price", "prev_close", "open", "high", "low", "change", "pct_change",
                     "volume_shares", "amount_cny", "turnover_rate", "volume_ratio",
                     "total_market_cap", "float_market_cap"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError("finite_number_or_null_required:" + name)
        if self.quote_status not in {"LIVE", "STALE", "CACHED", "UNAVAILABLE"}:
            raise ValueError("invalid_quote_status")
        if self.quote_time is not None and self.quote_time.tzinfo is None:
            raise ValueError("aware_quote_time_required")

    def to_dict(self):
        result = asdict(self)
        result["quote_time"] = self.quote_time.isoformat() if self.quote_time else None
        return result


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str | None
    exchange: str
    board: str


@dataclass
class SnapshotBatch:
    snapshots: dict[str, MarketSnapshot]
    requested_symbols: list[str]
    returned_symbols: list[str]
    valid_symbols: list[str]
    zero_price_symbols: list[str]
    missing_symbols: list[str]
    coverage_ratio: float
    valid_price_ratio: float
    latency: float
    source: str
    errors: list[str] = field(default_factory=list)
    timestamp_advanced: bool | None = None
    market_session: str = ""
    received_at: datetime | None = None

    def metrics(self):
        return {key: value for key, value in asdict(self).items() if key != "snapshots"}
