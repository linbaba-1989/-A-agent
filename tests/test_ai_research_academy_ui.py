from contextlib import nullcontext
from types import SimpleNamespace

from src.llm_router import load_role_config
from src.model_registry import ModelRegistry
from ui.pages import ai_research


def _ctx():
    def unexpected_model_call(*args, **kwargs):
        raise AssertionError("academy view must not call a model")

    return {"routes": load_role_config(), "registry": ModelRegistry(),
            "workforce": SimpleNamespace(few_shot=SimpleNamespace(enabled=False),
                                          analyze=unexpected_model_call)}


def test_capability_snapshot_uses_active_routes_and_local_case_library():
    snapshot = ai_research.capability_snapshot(_ctx(), {})
    assert snapshot["case_total"] == 46
    assert snapshot["case_counts"] == {
        "technical_analyst": 12, "fundamental_event_analyst": 8,
        "sentiment_analyst": 9, "risk_officer": 8, "chief_researcher": 9,
    }
    models = [row["model"] for row in snapshot["models"]]
    assert models[0:2] == ["DeepSeek V4-Pro", "Qwen 3.8 MAX"]
    assert models[2].startswith("Doubao")
    assert models[3:] == ["Kimi K3", "DeepSeek V4-Pro"]
    assert snapshot["default_enabled"] is False
    assert snapshot["enabled"] is False
    assert ai_research.capability_snapshot(_ctx(), {"ai_case_enhancement_enabled": True})["enabled"] is True
    assert ai_research.capability_snapshot(_ctx(), {"ai_case_enhancement_enabled": True,
                                                    "ai_case_enhancement_reset_pending": True})["enabled"] is False


class _FakeStreamlit:
    def __init__(self, preview=False):
        self.session_state = {}
        self.rendered = []
        self.preview = preview

    def title(self, value): self.rendered.append(str(value))
    def caption(self, value): self.rendered.append(str(value))
    def markdown(self, value, **kwargs): self.rendered.append(str(value))
    def info(self, value): self.rendered.append(str(value))
    def button(self, value, **kwargs): return self.preview
    def segmented_control(self, label, options, default=None, **kwargs): return default
    def columns(self, spec, **kwargs): return [nullcontext() for _ in spec]
    def tabs(self, names): return [nullcontext() for _ in names]


def test_empty_academy_still_shows_capabilities_and_safe_example(monkeypatch):
    fake = _FakeStreamlit(preview=True)
    monkeypatch.setattr(ai_research, "st", fake)
    rendered_reports = []
    monkeypatch.setattr(ai_research, "render_ai_report", rendered_reports.append)

    ai_research.render(_ctx())

    text = " ".join(fake.rendered)
    assert "五位AI员工" in text and "DeepSeek V4-Pro" in text
    assert "46 例" in text and "Technical" in text and "Chief" in text
    assert "Schema" in text and "P1.9.1" in text
    assert "Dynamic Few-shot" in text and "P1.9.2" in text
    assert "真实 A/B 评测" in text and "未开始" in text
    assert "动态检索默认" in text and "关闭" in text
    assert "示例预览" in text and "不会调用AI模型" in text
    assert len(rendered_reports) == 1
    assert rendered_reports[0]["example_preview"] is True
    assert rendered_reports[0]["chief_researcher"]["success"] is True
    assert "research_history" not in fake.session_state
