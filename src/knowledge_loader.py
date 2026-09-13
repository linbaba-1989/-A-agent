from pathlib import Path


ROLE_KNOWLEDGE = {
    "technical_analyst": ("market_rules.md", "technical_framework.md", "anti_hallucination.md"),
    "fundamental_event_analyst": ("fundamental_event_framework.md", "evidence_policy.md", "anti_hallucination.md"),
    "sentiment_analyst": ("market_rules.md", "sentiment_framework.md", "terminology.md", "anti_hallucination.md"),
    "risk_officer": ("market_rules.md", "risk_framework.md", "anti_hallucination.md"),
    "chief_researcher": ("chief_framework.md", "evidence_policy.md", "anti_hallucination.md"),
}


def load_role_knowledge(role: str, root: Path | None = None) -> str:
    base = root or Path(__file__).resolve().parents[1] / "knowledge" / "a_share"
    return "\n\n".join((base / name).read_text(encoding="utf-8") for name in ROLE_KNOWLEDGE[role])


def structured_prompt(role_text: str, knowledge: str) -> str:
    return "\n\n".join(("ROLE\n" + role_text, "OBJECTIVE\n基于输入事实完成本岗位A股研究。",
                           "A_SHARE_FRAMEWORK\n" + knowledge,
                           "AVAILABLE_FACTS\n仅使用随后用户消息中的事实与程序因子。",
                           "EVIDENCE_RULES\n逐项引用证据，区分事实、报道、推测和模型推理。",
                           "FORBIDDEN_INFERENCES\n禁止补造数字、事件、主力行为、确定收益或交易指令。",
                           "DECISION_PROCESS\n先数据完整性，再证据与冲突，最后结论和confidence。",
                           "OUTPUT_SCHEMA\n严格输出调用方指定JSON Schema，不增加字段。"))
