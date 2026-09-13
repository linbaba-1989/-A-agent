import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.agent import StockResearchAgent
from src.llm_router import load_role_config
from src.market_data_router import MarketDataRouter
from src.model_registry import ModelRegistry
from src.qmt_provider import QMTProvider
from src.scanner import MarketScanner
from ui.components.market_status import render_status_strip
from ui.components.sidebar import render_sidebar
from ui.components.topbar import render_topbar
from ui.pages import ai_research, backtest, dashboard, scanner as scanner_page, settings, stock_research, watchlist
from ui.theme import apply_theme
from ui.view_models import public_market_status

load_dotenv()
st.set_page_config(page_title="A-Agent｜AI量化研究终端", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")
apply_theme()


@st.cache_resource
def market_resources(qmt_path: str, qmt_port: int, token_configured: bool):
    router = MarketDataRouter(qmt_factory=lambda: QMTProvider(qmt_path=qmt_path, port=qmt_port))
    selection = router.select()
    market_scanner = MarketScanner(selection.provider) if selection.provider else None
    return router, selection, market_scanner


@st.cache_resource
def workforce_resource() -> StockResearchAgent:
    return StockResearchAgent()


def persisted_audit() -> dict | None:
    path = Path("outputs/diagnostics/xtdc_p010_audit.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("audit")
    except (OSError, ValueError, TypeError):
        return None


qmt_defaults = QMTProvider()
market_router, market_selection, market_scanner = market_resources(
    qmt_defaults.qmt_path, qmt_defaults.port, bool(os.getenv("XTDC_TOKEN", "").strip()))
provider = market_selection.provider
audit = getattr(provider, "universe_audit", None) if provider else None
audit = audit or persisted_audit()
status = public_market_status(market_selection, audit)
ctx = {"router": market_router, "selection": market_selection, "provider": provider,
       "scanner": market_scanner, "available": provider is not None, "status": status,
       "workforce": workforce_resource(), "registry": ModelRegistry(), "routes": load_role_config()}

page = render_sidebar()
symbol, analysis_mode = render_topbar(status)
if market_selection.status == "failed":
    st.error("行情服务不可用，实时筛选已停止。")

if page == "总览":
    dashboard.render(ctx)
elif page == "全A扫描":
    scanner_page.render(ctx)
elif page == "自选股":
    watchlist.render(ctx)
elif page == "个股研究":
    stock_research.render(ctx, symbol, analysis_mode)
elif page == "AI研究院":
    ai_research.render(ctx)
elif page == "策略回测":
    backtest.render(ctx)
elif page == "设置":
    settings.render(ctx)
render_status_strip(status)
