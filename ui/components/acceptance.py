"""Developer-only acceptance controls; never gate production navigation or SSE."""
import os
import streamlit as st

from src.acceptance_source import (MIN_ACCEPTANCE_SECONDS, acceptance_mode, acceptance_preflight,
                                    acceptance_running, worker_count)
from src.market_clock import beijing_now


def _save_choice(widget_key, durable_key):
    st.session_state[durable_key] = st.session_state[widget_key]


def _change_mode():
    st.session_state.acceptance_mode = st.session_state["_acceptance_mode_choice"]
    st.session_state.pop("acceptance_run", None)
    st.session_state.pop("acceptance_error", None)


def render_developer_tools():
    with st.expander("开发者工具", expanded=False):
        st.session_state["_acceptance_mode_choice"] = acceptance_mode(st.session_state, os.environ)
        st.toggle("验收模式", key="_acceptance_mode_choice", on_change=_change_mode)
        st.caption("默认关闭。开启后显示实盘验收工具；所有业务页面始终可用。")


def _start_acceptance(owner):
    if not acceptance_mode(st.session_state, os.environ):
        return
    now = beijing_now()
    result = acceptance_preflight(owner.selection, owner.feed, now=now, workers=worker_count())
    if not result["ready"]:
        return
    if not owner.verify_acceptance_connection():
        st.session_state.acceptance_error = "验收连接检查未通过；不影响查看已有行情和使用其他页面。"
        return
    now = beijing_now()
    result = acceptance_preflight(owner.selection, owner.feed, now=now, workers=worker_count())
    if not result["ready"]:
        return
    st.session_state.pop("acceptance_error", None)
    st.session_state.acceptance_run = {
        "generation": owner.generation, "started_at": now.timestamp(),
        "ends_at": now.timestamp() + MIN_ACCEPTANCE_SECONDS}
    st.session_state.streaming_beta_enabled = True
    st.session_state.realtime_enabled = True
    st.session_state.realtime_frequency = 2


def render_source_controls(ctx):
    locked = acceptance_mode(st.session_state, os.environ)
    # Durable user preference survives leaving Settings and leaving acceptance.
    st.session_state["_market_source_choice"] = st.session_state.get("market_source", "auto")
    st.selectbox("行情源选择", ["auto", "xtdatacenter", "qmt"], key="_market_source_choice",
                 format_func=lambda value: {"auto": "自动", "xtdatacenter": "XtDataCenter Token", "qmt": "QMT Local"}[value],
                 disabled=locked, on_change=_save_choice, args=("_market_source_choice", "market_source"))
    if locked:
        st.caption("验收模式使用 XtDataCenter Token；退出验收模式后恢复普通行情源选择。")
    if st.button("重新检查行情源"):
        ctx["source_resources"].close()
        st.session_state.pop("acceptance_run", None)
        st.rerun()


def render_acceptance_panel(ctx, page):
    if not acceptance_mode(st.session_state, os.environ):
        return False
    owner = ctx["source_resources"]
    now = beijing_now()
    result = acceptance_preflight(ctx["selection"], ctx.get("realtime_feed"), now=now,
                                  workers=worker_count())
    running = acceptance_running(st.session_state.get("acceptance_run"), result, owner.generation, now)
    if not running:
        st.session_state.pop("acceptance_run", None)
    with st.expander("开发者 / 实盘验收", expanded=False):
        st.write("Required Source: XtDataCenter Token")
        st.write(f"Active Source: {result['active_source']}")
        st.write(f"Fallback: {result['fallback']}")
        st.caption(f"market_session={result['market_session']} · 连续竞价剩余{result['remaining_continuous_seconds']:.0f}秒 · "
                   f"provider_init_count={result['provider_init_count']} · worker_count={result['worker_count']}")
        if not result["ready"] and not running:
            if result["market_session"] == "closed":
                st.info("当前已收盘，实时验收需在连续竞价时段运行。普通功能仍可使用。")
            elif result["market_session"] != "open":
                st.info("当前不在连续竞价时段，暂不能开始实时验收。普通功能仍可使用。")
            elif result["remaining_continuous_seconds"] < MIN_ACCEPTANCE_SECONDS:
                st.info("连续竞价剩余时间不足10分钟，暂不能开始本轮验收。")
            else:
                st.info("验收条件尚未满足；不影响普通功能。")
            st.caption("检查详情：" + ", ".join(result["reasons"]))
        if st.session_state.get("acceptance_error"):
            st.info(st.session_state.acceptance_error)
        st.button("Start Acceptance", disabled=not result["ready"] or running or page != "实时行情",
                  on_click=_start_acceptance, args=(owner,))
        if page != "实时行情" and result["ready"]:
            st.caption("请切换至实时行情，再点击 Start Acceptance。")
        if running:
            st.success("实盘验收运行中（10分钟）；普通页面与 Beta 继续使用共享行情。")
            if st.button("停止验收"):
                st.session_state.pop("acceptance_run", None)
                running = False
    ctx["acceptance_authorized"] = running
    ctx["acceptance_preflight"] = result
    return running
