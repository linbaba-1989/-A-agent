from urllib.parse import urlencode
import streamlit as st
import streamlit.components.v1 as components

from src.streaming_ui import StreamingUI
from src.acceptance_source import shared_beta_feed
from src.realtime_market import RealtimeMarketFeed
from src.realtime_presentation import beta_unavailable_message
from src.acceptance_source import TOKEN_SOURCE


@st.cache_resource
def streaming_resource(_feed, feed_identity: int) -> StreamingUI:
    # Identity prevents an old service attaching to a replacement feed. Reruns
    # retain the same resource. This adapter never constructs a Provider.
    return StreamingUI(_feed)


@st.cache_resource
def offline_token_feed():
    return RealtimeMarketFeed(None)


def render_streaming_beta(ctx, *, fast_only=False):
    feed = shared_beta_feed(ctx)
    offline = feed is None
    if feed is None:
        # Legacy disk fallback belongs only to an actual Token selection.
        selection = ctx.get("selection")
        if not (getattr(selection, "name", None) == TOKEN_SOURCE or
                getattr(selection, "required_source", None) == TOKEN_SOURCE):
            st.info(beta_unavailable_message(ctx))
            return
        feed = offline_token_feed()
        st.info("XtDataCenter 当前不可用；仅显示最近有效的 XtDataCenter Token 缓存。")
    owner = ctx.get("source_resources")
    if owner is not None and not offline:
        with owner.lock:
            if shared_beta_feed(ctx) is None:
                st.info("行情源已切换，请刷新页面以读取最近有效行情。")
                return
            if owner.streaming_service is None:
                owner.streaming_service = StreamingUI(feed)
            service = owner.streaming_service
    else:
        service = streaming_resource(feed, id(feed))
    enabled = int(bool(st.session_state.get("realtime_enabled", False)))
    interval = st.session_state.get("realtime_frequency", 2)
    # The Beta frame owns the live status presentation. Clear stale outer
    # labels rather than rerun the Streamlit page just to update its footer.
    for slot in ctx.get("status_slots", {}).values():
        slot.empty()
    ctx["streaming_status_owned"] = True
    current = "600498.SH"
    if service.fast is not None:
        from ui.view_models import normalize_symbol
        current = normalize_symbol(st.text_input("快速查看股票", value="600498.SH", key="fast_current_symbol"))
        from src.market_data.contracts import canonical_symbol
        try: current = canonical_symbol(current)
        except ValueError:
            st.info("请输入有效股票代码，如600498.SH。")
            return
        st.caption("单股/自选：Fast Lane，目标约1秒；全A/Top20：Full Market，保持原刷新周期。")
    else:
        st.caption("动态模式 Beta · 600498.SH / 涨幅 Top20 · 本机 SSE")
    developer = int(bool(ctx.get("acceptance_mode", False)))
    query = urlencode(dict(enabled=enabled, interval=interval, acceptance=developer,
        fast=int(service.fast is not None), fast_only=int(fast_only), current=current,
        watch=",".join(st.session_state.get("watchlist", ["600498.SH"])[:51])))
    components.iframe(f"{service.url}?{query}",
                      height=1000, scrolling=True)
