from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass
class ProviderStatus:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketDiagnostics:
    connected: bool
    xtquant_path: str | None
    xtquant_imported: bool = False
    rpc_request_success: bool = False
    full_tick_success: bool = False
    process_detected: bool = False
    processes: list[str] = field(default_factory=list)
    python_version: str = ""
    raw_error: str | None = None
    stock_pool_size: int = 0
    sh_count: int = 0
    sz_count: int = 0
    bj_count: int = 0
    full_tick_count: int = 0
    valid_quote_count: int = 0
    suspended_count: int = 0
    abnormal_count: int = 0
    invalid_quote_count: int = 0
    turnover_valid_count: int = 0
    speed_1m_valid_count: int = 0
    speed_3m_valid_count: int = 0
    speed_5m_valid_count: int = 0
    full_tick_seconds: float = 0.0
    history_init_seconds: float = 0.0
    realtime_indicator_seconds: float = 0.0
    filter_seconds: float = 0.0
    memory_mb: float | None = None
    elapsed_seconds: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    rows: list[dict[str, Any]]
    diagnostics: MarketDiagnostics
    unavailable_fields: list[str] = field(default_factory=list)
