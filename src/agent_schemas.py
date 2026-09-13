from pydantic import BaseModel, Field


class TechnicalReport(BaseModel):
    trend: str
    momentum: str
    volume_price: str
    support: list[float | str]
    resistance: list[float | str]
    bullish_signals: list[str]
    bearish_signals: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class FundamentalEventReport(BaseModel):
    confirmed_facts: list[str]
    events_to_verify: list[str]
    industry_changes: list[str]
    catalysts: list[str]
    risks: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class SentimentReport(BaseModel):
    market_sentiment: str
    sector_heat: str
    theme_strength: str
    news_sentiment: str
    capital_preference: str
    uncertainties: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class RiskReport(BaseModel):
    risk_points: list[str]
    valuation_risk: str
    technical_breakdown: str
    crowding_risk: str
    black_swan_risks: list[str]
    missing_data: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str


class ChiefReport(BaseModel):
    confirmed_facts: list[str]
    inferences: list[str]
    uncertainties: list[str]
    bull_case: list[str]
    bear_case: list[str]
    key_catalysts: list[str]
    key_risks: list[str]
    technical_view: str
    fundamental_view: str
    sentiment_view: str
    risk_view: str
    confidence: int = Field(ge=0, le=100)
    final_summary: str
