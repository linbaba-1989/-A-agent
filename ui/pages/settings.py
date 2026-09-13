import os
import streamlit as st


ROLE_NAMES = {"technical_analyst": "技术分析员", "fundamental_event_analyst": "基本面分析员",
              "sentiment_analyst": "情绪分析员", "risk_officer": "风险官", "chief_researcher": "总研究员"}


def render(ctx: dict) -> None:
    st.title("设置")
    market, models, employees, system = st.tabs(["行情", "AI模型", "AI员工", "系统"])
    with market:
        status = ctx["status"]
        cols = st.columns(4)
        cols[0].metric("当前 Provider", status["provider"])
        cols[1].metric("Raw Universe", status["raw_universe"])
        cols[2].metric("Active Universe", status["active_universe"])
        cols[3].metric("Valid Quotes", status["valid_quotes"])
        st.write(f"QMT fallback：{status['qmt_fallback']}")
        st.write(f"Token：configured={bool(os.getenv('XTDC_TOKEN', '').strip())}")
    with models:
        rows = [{"Provider": item["provider_name"], "模型": item["model_name"],
                 "配置": "已配置" if item["configured"] else "未配置",
                 "健康状态": "待体检", "最后测试延迟": "--"} for item in ctx["registry"].statuses()]
        st.dataframe(rows, hide_index=True, width="stretch")
    with employees:
        rows = []
        for role, route in ctx["routes"].items():
            candidate = route["candidates"][0]
            rows.append({"AI员工": ROLE_NAMES[role], "Provider": candidate["provider"],
                         "模型": candidate.get("model") or "当前 Endpoint"})
        st.dataframe(rows, hide_index=True, width="stretch")
    with system:
        st.write("A-Agent V1.0 UI P1.2")
        st.caption("研究辅助工具，不执行自动交易。")
