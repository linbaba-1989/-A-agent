import json
from .llm import ask

ROLES = [
    ("技术分析师", "只基于行情快照，判断趋势、关键价位、量价关系；未知信息要明确说明。"),
    ("基本面与事件研究员", "识别必须补充核实的财务、公告、产业链信息；禁止把猜测写成事实。"),
    ("市场情绪研究员", "评价交易拥挤度与情绪风险，给出需要观察的信号。"),
    ("风控官", "给出仓位、止损失效条件和不确定性；不能给确定收益承诺。"),
]

class StockResearchAgent:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def analyze(self, symbol: str, quote: dict) -> str:
        context = f"标的：{symbol}\n真实QMT快照：{json.dumps(quote, ensure_ascii=False)}"
        reports = []
        for title, instruction in ROLES:
            reports.append(f"### {title}\n{ask(self.model_name, instruction, context)}")
        synthesis = ask(
            self.model_name,
            "你是总研究员。基于各角色结论，输出：已确认事实、待验证点、交易观察计划、主要风险。不要编造数据，不构成投资建议。",
            context + "\n\n" + "\n\n".join(reports),
        )
        return "\n\n".join(reports) + f"\n\n## 总研究员汇总\n{synthesis}"
