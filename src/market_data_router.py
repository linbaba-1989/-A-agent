"""Market data provider selection with no simulated or third-party fallback."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .qmt_provider import QMTProvider
from .xtdc_provider import XtDataCenterProvider


@dataclass
class MarketDataSelection:
    provider: Any | None
    name: str
    status: str
    fallback: bool
    reason: str | None = None
    token_expiry: str = "unknown"
    qmt_fallback_status: str = "unknown"


class MarketDataRouter:
    def __init__(self, xtdc_factory: Callable[[], Any] = XtDataCenterProvider,
                 qmt_factory: Callable[[], Any] = QMTProvider):
        self.xtdc_factory = xtdc_factory
        self.qmt_factory = qmt_factory
        self.selection: MarketDataSelection | None = None

    def select(self) -> MarketDataSelection:
        token_provider = self.xtdc_factory()
        token_reason = "xtdc_token_missing"
        if getattr(token_provider, "configured", False):
            status = token_provider.check_connection()
            if status.ok:
                qmt_provider = self.qmt_factory()
                qmt_diagnostic = qmt_provider.connection_diagnostics()
                if hasattr(qmt_provider, "close"):
                    qmt_provider.close()
                qmt_status = "configured / available" if qmt_diagnostic.connected else "configured / unavailable"
                self.selection = MarketDataSelection(
                    token_provider, "XtDataCenter Token", "connected", False,
                    qmt_fallback_status=qmt_status,
                )
                return self.selection
            token_reason = status.message
        token_provider.close()
        qmt_provider = self.qmt_factory()
        diagnostic = qmt_provider.connection_diagnostics()
        if diagnostic.connected:
            self.selection = MarketDataSelection(
                qmt_provider, "QMT Local", "fallback", True, token_reason,
                qmt_fallback_status="configured / available",
            )
            return self.selection
        self.selection = MarketDataSelection(
            None, "unavailable", "failed", False,
            f"xtdc={token_reason}; qmt={diagnostic.message}",
            qmt_fallback_status="configured / unavailable",
        )
        return self.selection

    def require_provider(self) -> Any:
        selection = self.selection or self.select()
        if selection.provider is None:
            raise RuntimeError(f"market_data_unavailable: {selection.reason}")
        return selection.provider

    def close(self) -> None:
        if self.selection and self.selection.provider and hasattr(self.selection.provider, "close"):
            self.selection.provider.close()
