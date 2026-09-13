# 全A多模型分析 Agent

面向 A 股研究、盘中复盘和项目迭代的本地 Agent。

## 当前状态（V1.0 开发中）

- **多模型统一调用**：OpenAI / ChatGPT、DeepSeek、通义千问、Kimi、豆包
- **真实行情入口**：仅限本机 QMT（迅投 xtquant），断开时停止分析
- **全 A 筛选**：批量快照、分钟/日 K、涨速、均线、突破、VWAP、量比和透明评分
- **行情诊断**：报告股票池、`get_full_tick` 返回数、有效行情数和耗时
- **三级连接判定**：分别记录 xtquant 导入、基础 RPC 请求和真实股票 tick，不以进程或 `is_connected()` 单独判定
- **缓存边界**：合约信息与历史日 K 初始化一次；盘中每轮只更新全量 tick 和 10 分钟内存快照
- **分析工作流**：技术面、基本面/事件、情绪、风控四个角色分别输出，再由总研究员汇总
- **AI 员工路由**：五家 Provider 由 `config/agent_config.json` 为岗位配置主模型和备用模型
- **失败隔离**：主模型有限重试后自动切换备用模型；岗位全部失败时输出 `unavailable`
- **结构化输出**：各岗位使用 Pydantic 校验 JSON，格式错误最多修复一次
- **并行分析**：四个专业岗位并行执行，完成后再调用总研究员
- **用量审计**：调用元数据写入本机 `logs/model_usage.jsonl`，不记录 Prompt、FACT DATA 或 API Key
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

先按 [项目隔离 xtquant 安装说明](tools/xtquant_runtime/230825b/README.md)安装官方 `230825b` 运行时。项目不会上传或替换 QMT 安装目录中的 DLL、pyd 或 Python 文件。`QMT_PATH` 仅用于诊断页面定位和比较券商随附组件，生产行情固定从项目隔离目录加载；端口必须与本机 QMT 行情服务一致。模型 Key 仅在运行单股 AI 研究时需要。

AI 岗位路由可直接编辑 `config/agent_config.json`，无需修改 Python。启动和打开“AI员工”页只检查 Key 与配置，不调用任何模型；只有单股深度分析或用户点击“一键体检”时才发送请求。成本单价通过 `.env` 中各供应商的 `*_INPUT_COST_PER_MILLION` 和 `*_OUTPUT_COST_PER_MILLION` 配置，未配置时记录为 0，避免展示未经确认的价格。

启动 QMT 并登录行情后，可以先执行只读诊断：

```powershell
python scripts/qmt_diagnostics.py
```

诊断只有在基础 RPC 成功且测试股票包含有效 `lastPrice`、`lastClose` 和行情时间时才显示 `connected: true`。诊断还会真实比较市场代码全推与股票列表分批方案，并输出成交量单位证据、10 只股票样本和性能指标。

实时涨速只由连续 `get_full_tick` 快照计算。启动不足 1/3/5 分钟时分别显示 `unavailable`。实时 MA 使用此前完整交易日收盘和当前价格；突破高点只使用当前行情日前的完整日 K。软件不使用第三方行情源、模拟行情或交易接口。

换手率使用 `volume × 100 / FloatVolume × 100` 与 `pvolume / FloatVolume × 100` 交叉验证。股本无效或两套结果超出容差时显示 `unavailable`，不会输出未经验证的数值。

若需要区分外部 xtquant 解码问题和 QMT 内部数据问题，可在 QMT 模型编辑器中运行 `scripts/qmt_internal_capital_probe.py`。该探针只读取三只股票的合约信息，不包含交易调用。

证券状态优先读取日 K 的 `suspendFlag`，并显示为 `normal`、`suspended`、`resumed_today`、`invalid_quote` 或 `unknown`。`stockStatus`、`openInt`、`InstrumentStatus` 和 `IsTrading` 保留为原始诊断字段。

## 后续迭代

1. 用本机已登录 QMT 完成全 A 数量、行情权限与性能验收
2. 基于 QMT 行业分类和流通股本增加板块强度、换手率
3. 将 Agent 输出落库并做回测/复盘
4. 接入 GitHub Issues 作为“需求—实现—测试”的迭代看板
