import os
import streamlit as st
from ui.components.acceptance import render_source_controls, render_developer_tools
from ui.research_view import model_display


ROLE_NAMES = {"technical_analyst": "技术分析员", "fundamental_event_analyst": "基本面分析员",
              "sentiment_analyst": "情绪分析员", "risk_officer": "风险官", "chief_researcher": "总研究员"}


def render(ctx: dict) -> None:
    st.title("设置")
    market, models, employees, system = st.tabs(["行情", "AI模型", "AI员工", "系统"])
    with market:
        render_source_controls(ctx)
        status = ctx["status"]
        cols = st.columns(4)
        cols[0].metric("当前行情源", status["provider"])
        cols[1].metric("原始股票池", status["raw_universe"])
        cols[2].metric("可扫描股票池", status["active_universe"])
        cols[3].metric("有效行情", status["valid_quotes"])
        st.write(f"QMT 备用：{'可用' if status['qmt_fallback'].endswith('available') and 'unavailable' not in status['qmt_fallback'] else '不可用'}")
        st.write(f"Token：{'已配置' if bool(os.getenv('XTDC_TOKEN', '').strip()) else '未配置'}")
    with models:
        production_roles = {}
        for role, route in ctx["routes"].items():
            provider = route["candidates"][0]["provider"]
            production_roles.setdefault(provider, []).append(ROLE_NAMES[role])
        rows = []
        for item in ctx["registry"].statuses():
            roles = " / ".join(production_roles.get(item["provider_name"], []))
            state = "● 就绪" if item["configured"] else "○ 未配置"
            rows.append({"服务商": item["provider_name"], "模型": model_display(item["model_name"]) if item["model_name"] else "未指定",
                         "状态": state, "用途": f"生产模型：{roles}" if roles else "候选模型",
                         "成本": item["pricing_status"]})
        st.dataframe(rows, hide_index=True, width="stretch")
    with employees:
        rows = []
        for role, route in ctx["routes"].items():
            candidate = route["candidates"][0]
            rows.append({"AI员工": ROLE_NAMES[role], "服务商": candidate["provider"],
                         "模型": model_display(candidate.get("model")) if candidate.get("model") else "当前 Endpoint"})
        st.dataframe(rows, hide_index=True, width="stretch")
    with system:
        st.write("A-Agent V1.0 UI P1.2")
        st.caption("研究辅助工具，不执行自动交易。")
        render_developer_tools()
