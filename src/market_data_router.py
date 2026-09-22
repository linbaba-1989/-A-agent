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
    required_source: str | None = None
    fallback_enabled: bool = True
    token_configured: bool = False


class MarketDataRouter:
    def __init__(self, xtdc_factory: Callable[[], Any] = XtDataCenterProvider,
                 qmt_factory: Callable[[], Any] = QMTProvider):
        self.xtdc_factory = xtdc_factory
        self.qmt_factory = qmt_factory
        self.selection: MarketDataSelection | None = None

    def select(self, *, acceptance_source: str | None = None,
               preferred_source: str = "auto") -> MarketDataSelection:
        if acceptance_source not in {None, "xtdatacenter"}:
            raise ValueError("unsupported_acceptance_source")
        if preferred_source not in {"auto", "xtdatacenter", "qmt"}:
            raise ValueError("unsupported_market_source")
        if acceptance_source == "xtdatacenter" or preferred_source == "xtdatacenter":
            return self._select_token_only(acceptance_source is not None)
        if preferred_source == "qmt":
            qmt_provider = self.qmt_factory()
            diagnostic = qmt_provider.connection_diagnostics()
            if diagnostic.connected:
                self.selection = MarketDataSelection(qmt_provider, "QMT Local", "connected", False,
                                                     fallback_enabled=False, qmt_fallback_status="disabled")
            else:
                if hasattr(qmt_provider, "close"):
                    qmt_provider.close()
                self.selection = MarketDataSelection(None, "unavailable", "failed", False,
                                                     "qmt_unavailable", fallback_enabled=False)
            return self.selection
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

    def _select_token_only(self, acceptance: bool) -> MarketDataSelection:
        """Fail closed before any QMT factory/check; never log provider secrets."""
        provider = None
        configured = False
        reason = "xtdc_not_configured"
        try:
            provider = self.xtdc_factory()
            configured = bool(getattr(provider, "configured", False))
            if configured:
                result = provider.check_connection()
                if result.ok:
                    self.selection = MarketDataSelection(
                        provider, "XtDataCenter Token", "connected", False,
                        qmt_fallback_status="disabled", fallback_enabled=False,
                        required_source="XtDataCenter Token" if acceptance else None,
                        token_configured=True)
                    return self.selection
                reason = "xtdc_unavailable"
        except Exception as exc:
            reason = f"xtdc_check_failed:{type(exc).__name__}"
        finally:
            if provider is not None and (self.selection is None or self.selection.provider is not provider):
                provider.close()
        self.selection = MarketDataSelection(
            None, "unavailable", "SOURCE_BLOCKED" if acceptance else "failed", False, reason,
            qmt_fallback_status="disabled", fallback_enabled=False,
            required_source="XtDataCenter Token" if acceptance else None, token_configured=configured)
        return self.selection

    def require_provider(self) -> Any:
        selection = self.selection or self.select()
        if selection.provider is None:
            raise RuntimeError(f"market_data_unavailable: {selection.reason}")
        return selection.provider

    def close(self) -> None:
        if self.selection and self.selection.provider and hasattr(self.selection.provider, "close"):
            self.selection.provider.close()
