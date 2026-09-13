from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictReport(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TechnicalReport(StrictReport):
    trend: str
    momentum: str
    volume_price: str
    moving_average_structure: str
    support: list[float | str]
    resistance: list[float | str]
    breakout_status: str
    bullish_signals: list[str]
    bearish_signals: list[str]
    data_gaps: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class FundamentalEventReport(StrictReport):
    data_status: Literal["available", "partial", "unavailable"]
    confirmed_facts: list[str]
    available_fundamental_data: list[str]
    event_data: list[str]
    missing_data: list[str]
    possible_implications: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class SentimentReport(StrictReport):
    data_status: Literal["available", "partial", "unavailable"]
    confirmed_market_signals: list[str]
    momentum_sentiment: str
    volume_sentiment: str
    crowding_risk: str
    missing_sentiment_data: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class RiskReport(StrictReport):
    technical_risks: list[str]
    data_quality_risks: list[str]
    positioning_risks: list[str]
    bull_case_challenges: list[str]
    invalid_assumptions: list[str]
    missing_information: list[str]
    risk_level: str
    confidence: int = Field(ge=0, le=100)
    summary: str


class ChiefReport(StrictReport):
    status: Literal["complete", "degraded"]
    missing_roles: list[str]
    confirmed_facts: list[str]
    data_gaps: list[str]
    bull_case: list[str]
    bear_case: list[str]
    key_catalysts: list[str]
    key_risks: list[str]
    technical_view: str
    fundamental_view: str
    sentiment_view: str
    risk_view: str
    points_of_agreement: list[str]
    points_of_disagreement: list[str]
    confidence: int = Field(ge=0, le=100)
    final_summary: str
