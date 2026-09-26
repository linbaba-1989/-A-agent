"""Opt-in foundation entry. Existing app/router behavior is intentionally untouched."""
import os
from .free_provider import FreeMarketDataProvider

ENV_NAME = "A_AGENT_MARKET_DATA_MODE"


def market_data_mode(environ=None):
    mode = (os.environ if environ is None else environ).get(ENV_NAME, "auto").strip().lower()
    if mode not in {"auto", "free", "xtdc", "qmt"}:
        raise ValueError("unsupported_market_data_mode")
    return mode


def create_market_provider(*, mode=None, environ=None, free_factory=FreeMarketDataProvider,
                           router_factory=None):
    selected = market_data_mode({ENV_NAME: mode}) if mode is not None else market_data_mode(environ)
    if selected == "free":
        return free_factory()  # No import or construction of paid providers.
    if router_factory is None:
        from ..market_data_router import MarketDataRouter
        router_factory = MarketDataRouter
    router = router_factory()
    selection = router.select(preferred_source={"auto": "auto", "xtdc": "xtdatacenter", "qmt": "qmt"}[selected])
    if selection.provider is None:
        raise RuntimeError("market_data_unavailable")
    return selection.provider
