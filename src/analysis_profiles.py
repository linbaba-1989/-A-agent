"""Research-depth profiles independent from prompts and market data."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisProfile:
    name: str
    label: str


PROFILES = {
    "standard": AnalysisProfile("standard", "标准"),
    "deep": AnalysisProfile("deep", "深度"),
    "max": AnalysisProfile("max", "MAX"),
}


def get_profile(name: str) -> AnalysisProfile:
    if name not in PROFILES:
        raise ValueError(f"unknown_analysis_mode: {name}")
    return PROFILES[name]
