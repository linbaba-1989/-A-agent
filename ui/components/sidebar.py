import streamlit as st


PAGES = ["▦  总览", "⌕  全A扫描", "☆  自选股", "▤  个股研究", "◆  AI研究院", "↗  策略回测", "⚙  设置"]
PAGE_NAMES = {item: item.split("  ", 1)[-1] for item in PAGES}


def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("**A-Agent**")
        st.caption("A股量化研究终端")
        requested = st.session_state.pop("nav_page", None)
        requested_item = next((item for item, name in PAGE_NAMES.items() if name == requested), None)
        if requested_item:
            st.session_state.main_navigation = requested_item
        page = st.radio("主导航", PAGES, label_visibility="collapsed", key="main_navigation")
        st.divider()
        st.caption("研究辅助 · 不执行交易")
    return PAGE_NAMES[page]
