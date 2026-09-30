import streamlit as st


def render(ctx: dict) -> None:
    st.title("策略回测")
    st.info("P3 本地行为模式的样本外回测已在 Behavior Lab 提供。这里不生成即时策略或模拟业绩。")
    if st.button("打开 Behavior Lab"):
        st.session_state.nav_page = "Behavior Lab"
        st.rerun()
