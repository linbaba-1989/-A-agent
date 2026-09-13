import os
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from src.agent import StockResearchAgent
from src.llm_router import load_role_config
from src.model_registry import ModelRegistry
from src.market_data_router import MarketDataRouter
from src.qmt_provider import QMTProvider
from src.scanner import MarketScanner

load_dotenv()
st.set_page_config(page_title="全A多模型分析 Agent", page_icon="📈", layout="wide")
st.title("📈 全A多模型分析 Agent")
st.caption("本机 QMT/xtquant 真实行情；研究辅助工具，不执行任何交易。")

@st.cache_resource
def market_resources(qmt_path: str, qmt_port: int, token_configured: bool):
    router = MarketDataRouter(qmt_factory=lambda: QMTProvider(qmt_path=qmt_path, port=qmt_port))
    selection = router.select()
    scanner = MarketScanner(selection.provider) if selection.provider else None
    return router, selection, scanner


qmt_defaults = QMTProvider()
market_router, market_selection, scanner = market_resources(
    qmt_defaults.qmt_path, qmt_defaults.port, bool(os.getenv("XTDC_TOKEN", "").strip())
)
provider = market_selection.provider
status_ok = provider is not None
if market_selection.status == "connected":
    st.success("当前Provider：XtDataCenter Token｜🟢 已连接｜Token剩余有效期：unknown")
elif market_selection.status == "fallback":
    st.warning("当前Provider：QMT Local｜🟡 fallback｜Token剩余有效期：unknown")
else:
    st.error(f"行情源不可用，实时筛选已停止：{market_selection.reason}")
st.caption(f"Fallback：QMT Local｜{market_selection.qmt_fallback_status}")
provider_audit = getattr(provider, "universe_audit", None) if provider else None
if provider_audit:
    st.caption(f"行情状态：{provider_audit['market_status']}｜最新行情时间：{provider_audit['latest_quote_time']}")

scan_tab, stock_tab, employees_tab, diagnostic_tab = st.tabs(["全A实时筛选", "单股研究", "AI员工", "行情诊断"])

@st.cache_resource
def workforce_resource() -> StockResearchAgent:
    return StockResearchAgent()

workforce = workforce_resource()

with scan_tab:
    top_n = st.number_input("显示数量", min_value=10, max_value=100, value=30, step=10)
    if st.button("运行全A真实行情筛选", type="primary", disabled=not status_ok):
        progress = st.progress(0, text="正在初始化历史指标……")
        def update_progress(value, message):
            progress.progress(min(float(value), 1.0), text=message)
        with st.spinner("正在从本机 QMT 读取全 A 行情与 K 线…"):
            result = scanner.scan(int(top_n), progress_callback=update_progress)
        progress.progress(1.0, text="初始化及扫描完成")
        metrics = result.diagnostics
        audit = getattr(provider, "universe_audit", None)
        cols = st.columns(5)
        cols[0].metric("原始股票池", audit["raw_count"] if audit else metrics.stock_pool_size)
        cols[1].metric("可扫描股票池", metrics.stock_pool_size)
        cols[2].metric("get_full_tick 返回", metrics.full_tick_count)
        cols[3].metric("有效行情", metrics.valid_quote_count)
        cols[4].metric("筛选耗时", f"{metrics.elapsed_seconds:.2f}s")
        if audit:
            st.caption(f"行情状态：{audit['market_status']}｜最新行情时间：{audit['latest_quote_time']}")
            with st.expander(f"缺失 tick 审计：{audit['missing_tick_count']} 只"):
                st.json({"按市场": audit["missing_by_market"], "原因分类": audit["missing_reason_counts"],
                         "前100只": audit["missing_tick_first_100"],
                         "无效 tick 数量": audit["invalid_returned_count"],
                         "无效 tick 代码": audit["invalid_returned_symbols"]})
        if result.unavailable_fields:
            st.warning("QMT 基础数据无法可靠计算：" + "、".join(result.unavailable_fields))
        display_rows = pd.DataFrame(result.rows)
        if "turnover_rate" in display_rows:
            display_rows["turnover_rate"] = pd.to_numeric(display_rows["turnover_rate"], errors="coerce").round(4)
        st.dataframe(display_rows, width="stretch", hide_index=True)

with stock_tab:
    max_mode = st.checkbox("MAX 深度研究模式", value=False,
                           help="增加响应时间和 Token/API 成本，适合重大持仓、深度研究或高风险决策。")
    symbol = st.text_input("股票代码", "600498.SH", help="例如：600498.SH、000001.SZ")
    if st.button("读取真实行情并分析", disabled=not status_ok):
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
                st.json(workforce.analyze(normalized_symbol, quote, "max" if max_mode else "standard"))

with employees_tab:
    st.subheader("AI 员工管理")
    employee_max_mode = st.checkbox("MAX 深度研究模式", value=False, key="employee_max_mode")
    if employee_max_mode:
        st.warning("🔥 MAX 深度研究模式：技术分析员与总研究员使用 DeepSeek V4-Pro / MAX。该模式会增加响应时间和 Token / API 成本。")
    else:
        st.info("研究模式：标准；技术分析员与总研究员使用 DeepSeek V4-Pro / High。")
    routes = load_role_config()
    registry = ModelRegistry()
    role_names = {"technical_analyst": "技术分析员", "fundamental_event_analyst": "基本面/事件分析员",
                  "sentiment_analyst": "市场情绪分析员", "risk_officer": "风控官",
                  "chief_researcher": "总研究员"}
    usage = workforce.router.tracker.summary()
    employee_rows = []
    for role, route in routes.items():
        candidates = route["candidates"]
        primary_candidate = candidates[0]
        primary_name = primary_candidate.get("model") or os.getenv(primary_candidate.get("model_env", ""), "当前 Endpoint")
        fallback_names = [item.get("model") or os.getenv(item.get("model_env", ""), "当前 Endpoint")
                          for item in candidates[1:]]
        recent = workforce.router.last_results.get(role)
        primary = registry.get(primary_candidate["provider"])
        state = "⚪ 未配置" if not primary.configured else "🟢 待命"
        runtime_state = workforce.router.role_states.get(role)
        if runtime_state == "working":
            state = "🔵 工作中"
        if recent and runtime_state != "working":
            state = "🟢 待命" if recent.success else "🔴 异常"
            if recent.fallback:
                state = "🟡 fallback"
        totals = usage.get(role, {})
        employee_rows.append({"员工岗位": role_names[role], "主模型": primary_name,
                              "备用模型": " → ".join(fallback_names),
                              "工作状态": state, "最近任务": recent.analysis_id if recent else "-",
                              "最近耗时": recent.latency if recent else 0, "最近Token": recent.total_tokens if recent else 0,
                              "累计Token": int(totals.get("total_tokens", 0)),
                              "累计估算成本": totals.get("estimated_cost", 0.0),
                              "错误": recent.error if recent else None})
    st.dataframe(pd.DataFrame(employee_rows), width="stretch", hide_index=True)
    provider_rows = [{"Provider": item["provider_name"], "Model": item["model_name"],
                      "状态": ("⚪ 暂停 / 未启用" if not item["enabled"] else
                              "🟢 已配置 / 待命" if item["configured"] else "🔴 未配置")}
                     for item in registry.statuses()]
    st.dataframe(pd.DataFrame(provider_rows), width="stretch", hide_index=True)
    st.caption("页面加载只检查配置，不发送模型请求。")
    if st.button("一键体检"):
        with st.spinner("每个已配置 Provider 发送一次极短 JSON 请求…"):
            with ThreadPoolExecutor(max_workers=5) as pool:
                checks = list(pool.map(workforce.router.healthcheck, registry.providers))
        st.dataframe(pd.DataFrame(checks), width="stretch", hide_index=True)

with diagnostic_tab:
    st.subheader("xtquant 运行时来源")
    try:
        runtime_attr = getattr(provider, "runtime_info", {})
        runtime = runtime_attr() if callable(runtime_attr) else runtime_attr
        st.json(runtime)
    except Exception as exc:
        st.error(f"xtquant 组件检查失败，生产运行已停止：{exc}")
    if provider:
        st.json(provider.connection_diagnostics().to_dict())
    else:
        st.error(market_selection.reason)
    if st.button("执行完整行情诊断", disabled=not status_ok or not hasattr(provider, "diagnose")):
        with st.spinner("验证股票池与全量 get_full_tick…"):
            st.json(provider.diagnose(include_market_sample=True).to_dict())
    if st.button("比较全市场与分批 get_full_tick", disabled=not status_ok):
        with st.spinner("正在执行两种真实全市场读取方式…"):
            st.json(scanner.compare_tick_strategies())
