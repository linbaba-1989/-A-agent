"""Causal local behavior research; no network or order submission."""
from .swing_detector import SwingDetector

def __getattr__(name):
    if name == "BehaviorEngine":
        from .pipeline import BehaviorEngine
        return BehaviorEngine
    raise AttributeError(name)
