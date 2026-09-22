import streamlit as st
import streamlit.components.v1 as components

from src.streaming_ui import StreamingUI
from src.acceptance_source import shared_beta_feed
from src.realtime_market import RealtimeMarketFeed


@st.cache_resource
def streaming_resource(_feed, feed_identity: int) -> StreamingUI:
    # Identity prevents an old service attaching to a replacement feed. Reruns
    # retain the same resource. This adapter never constructs a Provider.
    return StreamingUI(_feed)


@st.cache_resource
def offline_token_feed():
    return RealtimeMarketFeed(None)


def render_streaming_beta(ctx):
    feed = shared_beta_feed(ctx)
    offline = feed is None
    if feed is None:
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
    st.caption("动态模式 Beta · 600498.SH / 涨幅 Top20 · 本机 SSE · 状态随共享行情更新")
    developer = int(bool(ctx.get("acceptance_mode", False)))
    components.iframe(f"{service.url}?enabled={enabled}&interval={interval}&acceptance={developer}",
                      height=1000, scrolling=True)
