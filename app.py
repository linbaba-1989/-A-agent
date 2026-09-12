import os
import streamlit as st
from dotenv import load_dotenv
from src.agent import StockResearchAgent
from src.qmt_provider import QMTProvider

load_dotenv()
st.set_page_config(page_title="全A多模型分析 Agent", page_icon="📈", layout="wide")
st.title("📈 全A多模型分析 Agent")
st.caption("QMT 真实行情 + 多模型研究协作。仅供研究，不构成投资建议。")

with st.sidebar:
    model = st.selectbox("分析模型", ["deepseek", "qwen", "kimi", "doubao", "openai"],
                         index=["deepseek", "qwen", "kimi", "doubao", "openai"].index(os.getenv("DEFAULT_MODEL", "deepseek")))
    symbol = st.text_input("股票代码", "600498.SH", help="例如：600498.SH、000001.SZ")
    run = st.button("开始分析", type="primary")

provider = QMTProvider()
status = provider.status()
if status.ok:
    st.success(f"QMT 已连接：{status.message}")
else:
    st.warning(f"QMT 未连接：{status.message}。请配置 .env 后重启；不会显示伪实时数据。")

if run:
    quote = provider.get_quote(symbol)
    if not quote:
        st.error("未获得真实 QMT 行情，分析已停止。请确认代码、QMT 服务和行情权限。")
    else:
        st.subheader(f"{symbol} 行情快照")
        st.json(quote)
        with st.spinner("四角色研究中…"):
            result = StockResearchAgent(model).analyze(symbol, quote)
        st.subheader("综合研究结论")
        st.markdown(result)
