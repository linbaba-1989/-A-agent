# 全A多模型分析 Agent

面向 A 股研究、盘中复盘和项目迭代的本地 Agent。

## V1 已实现

- **多模型统一调用**：OpenAI / ChatGPT、DeepSeek、通义千问、Kimi、豆包
- **真实行情入口**：QMT（迅投 xtquant），支持全 A 股票列表、最新行情快照
- **分析工作流**：技术面、基本面/事件、情绪、风控四个角色分别输出，再由总研究员汇总
- **可审计输出**：每次分析带行情来源、时间、模型与风险提示
- **隐私**：API Key 仅放在本机 `.env`，不会上传 GitHub

> 研究辅助工具，不构成投资建议。行情“实时性”取决于你的 QMT 订阅权限与本地 QMT 服务状态。

## 快速启动（Windows + QMT）

```powershell
git clone https://github.com/linbaba-1989/-A-agent.git
cd -A-agent
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
streamlit run app.py
```

编辑 `.env`：至少填写一个模型 Key；如使用 QMT，再填写 QMT 路径和端口。

## 后续迭代

1. 将现有 QMT 秒级行情模块接入 `qmt_provider.py`
2. 增加全市场筛选策略、板块强弱、资金流与自选股
3. 将 Agent 输出落库并做回测/复盘
4. 接入 GitHub Issues 作为“需求—实现—测试”的迭代看板
