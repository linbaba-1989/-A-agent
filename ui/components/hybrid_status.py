"""Visible capability sources, with real quote status rather than connection claims."""
import streamlit as st


def render_hybrid_status(provider):
    states = provider.provider_status()
    st.caption("Hybrid ｜ " + " ｜ ".join(f"{k}: {states[k]}" for k in ("realtime", "history", "universe", "index")))
    scope = provider.get_stock_sectors('600498.SH')
    stale = scope.get('stale_members', 0)
    stale_text = f" · stale cache={stale}" if stale else ""
    st.caption(f"Sector cache: {scope['covered']}/{scope['total']} · {scope['quality_status']} · "
               f"unsupported={scope.get('unsupported_total', 0)}{stale_text}（支持范围外成员，不作为网络失败重试）")
    if states["official_auth"] == "AUTH_NOT_CONFIGURED":
        st.warning("Hithink AUTH_NOT_CONFIGURED：实时仍使用腾讯/新浪；日K可降级到 AKShare/BaoStock。")


def render_indices(provider, background=False):
    batch = provider.cached_index_snapshot() if background else provider.get_index_snapshot()
    columns = st.columns(3)
    for column, (symbol, name) in zip(columns, (("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"))):
        quote = batch.snapshots.get(symbol) if batch else None
        with column:
            st.metric(name, "--" if quote is None or quote.price is None else f"{quote.price:.2f}",
                      None if quote is None or quote.pct_change is None else f"{quote.pct_change:+.2f}%")
            st.caption(f"Hithink · {quote.quote_status if quote else 'UNAVAILABLE'} · "
                       f"{quote.quote_time.isoformat() if quote and quote.quote_time else '--'}")
            evidence = (getattr(batch, "provider_evidence", {}) or {}).get("index_observations", {}).get(symbol, {})
            if evidence:
                st.caption(f"{evidence.get('availability')} · 页级时间 {evidence.get('page_timestamp') or '--'}；非逐标的成交时间，未验证LIVE")
    if batch and batch.capability_status not in {"SUCCESS", "CACHED"}:
        st.caption("Index DEGRADED: " + batch.capability_status)


def render_memberships(provider, symbol):
    data = provider.get_stock_sectors(symbol)
    with st.expander("行业 / 概念（Hithink）"):
        stale = data.get('stale_members', 0)
        stale_text = f" · stale cache={stale}" if stale else ""
        st.caption(f"成员缓存 {data['covered']}/{data['total']} · {data['quality_status']}{stale_text} · "
                   f"同步状态 {provider.sector_sync_status}；未缓存不等于不属于任何板块。")
        if data.get('unsupported_total'):
            st.caption(f"支持范围 {data['cached_supported']}/{data['supported_total']}；"
                       f"{data['unsupported_total']} 项 UNSUPPORTED_MARKET，包含当前不支持的市场成员。")
        if data["groups"]:
            st.dataframe(data["groups"], hide_index=True)
        if st.button("后台同步行业/概念成员", key="hybrid_sync_sectors"):
            provider.sync_sectors()
        code = st.text_input("查询 THS 指数成员", placeholder="881101.TI")
        if st.button("读取成员", key="hybrid_sector_members") and code:
            try:
                result = provider.get_sector_members(code.strip())
                st.caption(result.status)
                if result.status == 'UNSUPPORTED_MARKET':
                    st.caption('该板块含当前不支持的市场成员，未返回空成员表。')
                elif result.data:
                    st.dataframe(result.data.get("item", []), hide_index=True)
            except ValueError:
                st.warning("请输入目录中的完整指数代码。")
