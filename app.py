import os
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from src.agent import StockResearchAgent
from src.qmt_provider import QMTProvider
from src.scanner import MarketScanner

load_dotenv()
st.set_page_config(page_title="全A多模型分析 Agent", page_icon="📈", layout="wide")
st.title("📈 全A多模型分析 Agent")
st.caption("本机 QMT/xtquant 真实行情；研究辅助工具，不执行任何交易。")

provider = QMTProvider()
status = provider.status()
if status.ok:
    st.success(status.message)
else:
    st.error(f"QMT 未连接，实时筛选已停止：{status.message}")

scan_tab, stock_tab, diagnostic_tab = st.tabs(["全A实时筛选", "单股研究", "行情诊断"])

@st.cache_resource
def scanner_resource(qmt_path: str, qmt_port: int) -> MarketScanner:
    return MarketScanner(QMTProvider(qmt_path=qmt_path, port=qmt_port))

scanner = scanner_resource(provider.qmt_path, provider.port)

with scan_tab:
    top_n = st.number_input("显示数量", min_value=10, max_value=100, value=30, step=10)
    if st.button("运行全A真实行情筛选", type="primary", disabled=not status.ok):
        progress = st.progress(0, text="正在初始化历史指标……")
        def update_progress(value, message):
            progress.progress(min(float(value), 1.0), text=message)
        with st.spinner("正在从本机 QMT 读取全 A 行情与 K 线…"):
            result = scanner.scan(int(top_n), progress_callback=update_progress)
        progress.progress(1.0, text="初始化及扫描完成")
        metrics = result.diagnostics
        cols = st.columns(4)
        cols[0].metric("股票池", metrics.stock_pool_size)
        cols[1].metric("get_full_tick 返回", metrics.full_tick_count)
        cols[2].metric("有效行情", metrics.valid_quote_count)
        cols[3].metric("筛选耗时", f"{metrics.elapsed_seconds:.2f}s")
        if result.unavailable_fields:
            st.warning("QMT 基础数据无法可靠计算：" + "、".join(result.unavailable_fields))
        display_rows = pd.DataFrame(result.rows)
        if "turnover_rate" in display_rows:
            display_rows["turnover_rate"] = pd.to_numeric(display_rows["turnover_rate"], errors="coerce").round(4)
        st.dataframe(display_rows, use_container_width=True, hide_index=True)

with stock_tab:
    model = st.selectbox("分析模型", ["deepseek", "qwen", "kimi", "doubao", "openai"],
                         index=["deepseek", "qwen", "kimi", "doubao", "openai"].index(os.getenv("DEFAULT_MODEL", "deepseek")))
    symbol = st.text_input("股票代码", "600498.SH", help="例如：600498.SH、000001.SZ")
    if st.button("读取真实行情并分析", disabled=not status.ok):
        normalized_symbol = symbol.strip().upper()
        quote = provider.get_quote(normalized_symbol)
        if not quote:
            st.error("未获得有效 QMT 行情，分析已停止。")
        else:
            st.json(quote)
            history = provider.get_history([normalized_symbol], "1d", 60).get(normalized_symbol)
            if history is not None and not history.empty:
                if "time" in history.columns:
                    st.line_chart(history, x="time", y="close")
                else:
                    st.line_chart(history["close"])
            with st.spinner("研究中…"):
                st.markdown(StockResearchAgent(model).analyze(normalized_symbol, quote))

with diagnostic_tab:
    st.subheader("xtquant 运行时来源")
    try:
        st.json(provider.runtime_info())
    except Exception as exc:
        st.error(f"xtquant 组件检查失败，生产运行已停止：{exc}")
    st.json(provider.connection_diagnostics().to_dict())
    if st.button("执行完整行情诊断", disabled=not status.ok):
        with st.spinner("验证股票池与全量 get_full_tick…"):
            st.json(provider.diagnose(include_market_sample=True).to_dict())
    if st.button("比较全市场与分批 get_full_tick", disabled=not status.ok):
        with st.spinner("正在执行两种真实全市场读取方式…"):
            st.json(scanner.compare_tick_strategies())
