import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from src.agent import StockResearchAgent
from src.acceptance_source import SourceResources, acceptance_mode, source_request
from src.llm_router import load_role_config
from src.market_clock import beijing_now, should_fetch_quotes
from src.market_data_router import MarketDataRouter
from src.model_registry import ModelRegistry
from src.qmt_provider import QMTProvider
from src.realtime_market import RealtimeMarketFeed
from src.scanner import MarketScanner
from ui.components.market_status import render_status_strip
from ui.components.acceptance import render_acceptance_panel
from ui.components.sidebar import render_sidebar
from ui.components.topbar import render_topbar
from ui.pages import ai_research, backtest, dashboard, realtime_market, scanner as scanner_page, settings, stock_research, watchlist
from ui.theme import apply_theme
from ui.view_models import public_market_status, quote_status_display

load_dotenv()
st.set_page_config(page_title="A-Agent｜AI量化研究终端", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")
apply_theme()


@st.cache_resource
def market_resources():
    return SourceResources(
        lambda: MarketDataRouter(qmt_factory=QMTProvider),
        MarketScanner, RealtimeMarketFeed)


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


try:
    requested_source = source_request(st.session_state, os.environ)
except ValueError as exc:
    st.error("SOURCE_BLOCKED: " + str(exc))
    st.stop()
source_resources = market_resources()
source_resources.activate(requested_source)
market_router, market_selection = source_resources.router, source_resources.selection
market_scanner, realtime_feed = source_resources.scanner, source_resources.feed
provider = market_selection.provider
audit = getattr(provider, "universe_audit", None) if provider else None
audit = audit or persisted_audit()
clock_now = beijing_now()
status = public_market_status(market_selection, audit, now=clock_now)

# A persisted universe audit is useful for counts, but its market status and
# quote timestamp are not a clock.  On startup/entry to auction or continuous
# trading, make an actual provider request before rendering the status bar.
if realtime_feed and should_fetch_quotes(status["market_session"]):
    previous_timestamp = st.session_state.get("realtime_quote_timestamp")
    try:
        probe_rows = realtime_feed.ensure_fresh_provider_probe(status["market_session"], now=clock_now)
    except Exception:
        probe_rows = []
    probe_timestamps = [row.get("quote_timestamp") for row in probe_rows if row.get("quote_timestamp") is not None]
    latest_timestamp = max(probe_timestamps) if probe_timestamps else realtime_feed.last_quote_timestamp
    if latest_timestamp is not None:
        status = quote_status_display(status, latest_timestamp, previous_timestamp, now=beijing_now(),
                                      market_session_value=status["market_session"], feed=realtime_feed)
        st.session_state.realtime_quote_timestamp = latest_timestamp
    st.session_state.realtime_quote_status = status["quote_status"]
    st.session_state.realtime_last_quote_time = status["last_quote_time"]
elif realtime_feed:
    # Lunch/pre-open/closed may use the last real quote for display, but never
    # turn that cached quote into an open-session request or a LIVE claim.
    realtime_feed.ensure_closed_snapshot(status["market_session"], now=clock_now)
    latest_timestamp = realtime_feed.last_quote_timestamp or status.get("last_quote_timestamp")
    if latest_timestamp is not None:
        status = quote_status_display(status, latest_timestamp,
                                      st.session_state.get("realtime_quote_timestamp"), now=clock_now,
                                      market_session_value=status["market_session"])
    st.session_state.realtime_quote_status = status["quote_status"]
    st.session_state.realtime_last_quote_time = status["last_quote_time"]
ctx = {"router": market_router, "selection": market_selection, "provider": provider,
       "scanner": market_scanner, "available": provider is not None, "status": status,
       "realtime_feed": realtime_feed,
       "source_resources": source_resources, "source_request": requested_source,
       "acceptance_mode": acceptance_mode(st.session_state, os.environ),
       "workforce": workforce_resource(), "registry": ModelRegistry(), "routes": load_role_config()}

page = render_sidebar()
symbol, analysis_mode, topbar_status_slot = render_topbar(status)
status_strip_slot = st.empty()
ctx["status_slots"] = {"topbar": topbar_status_slot, "status_strip": status_strip_slot}
if market_selection.status in {"failed", "SOURCE_BLOCKED"}:
    st.error("行情服务不可用，实时筛选已停止。")

if ctx["acceptance_mode"]:
    render_acceptance_panel(ctx, page)

if page == "总览":
    dashboard.render(ctx)
elif page == "实时行情":
    realtime_market.render(ctx)
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
if not ctx.get("streaming_status_owned"):
    render_status_strip(status, target=status_strip_slot)
