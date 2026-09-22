from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHORT_TERM_HORIZON = "当前至未来1~5个交易日的交易结构"
MID_TERM_HORIZON = "约2~8周的趋势结构"
RESEARCH_TIME_HORIZONS = {"short_term": SHORT_TERM_HORIZON, "mid_term": MID_TERM_HORIZON}

View = Literal["bullish", "neutral_bullish", "neutral", "neutral_bearish", "bearish", "unavailable"]
TrendState = Literal["strong_up", "uptrend", "range_up", "range", "range_down", "downtrend",
                     "strong_down", "unclear", "unavailable"]
RiskLevel = Literal["low", "medium", "high", "very_high", "unavailable"]
EvidenceQuality = Literal["high", "medium", "low", "insufficient"]
DataCompleteness = Literal["complete", "partial", "insufficient"]
EvidenceStatus = Literal["confirmed", "derived", "unavailable"]


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: str
    status: EvidenceStatus
    source: str | None = None


class InvalidationCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str
    timeframe: str
    evidence_source: str


class KeyConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str
    description: str
    roles: list[str]


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
    short_term_view: View = "unavailable"
    mid_term_view: View = "unavailable"
    trend_state: TrendState = "unavailable"
    ma_structure: str = "unavailable"
    volume_price_state: str = "unavailable"
    volatility_state: str = "unavailable"
    breakout_state: str = "unavailable"
    support_levels: list[float | str] = Field(default_factory=list)
    resistance_levels: list[float | str] = Field(default_factory=list)
    technical_drivers: list[str] = Field(default_factory=list)
    technical_risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = "insufficient"
    confidence_cap: int | None = Field(default=None, ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_within_cap(self):
        if self.confidence_cap is not None and self.confidence > self.confidence_cap:
            raise ValueError("confidence_exceeds_confidence_cap")
        return self


class FundamentalEventReport(StrictReport):
    data_status: Literal["available", "partial", "unavailable"]
    confirmed_facts: list[str]
    available_fundamental_data: list[str]
    event_data: list[str]
    missing_data: list[str]
    possible_implications: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str
    fundamental_view: View = "unavailable"
    event_view: View = "unavailable"
    earnings_status: str = "unavailable"
    valuation_status: str = "unavailable"
    industry_status: str = "unavailable"
    company_event_status: str = "unavailable"
    confirmed_events: list[str] = Field(default_factory=list)
    unconfirmed_events: list[str] = Field(default_factory=list)
    positive_factors: list[str] = Field(default_factory=list)
    negative_factors: list[str] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = "insufficient"
    confidence_cap: int | None = Field(default=None, ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_within_cap(self):
        if self.confidence_cap is not None and self.confidence > self.confidence_cap:
            raise ValueError("confidence_exceeds_confidence_cap")
        return self


class SentimentReport(StrictReport):
    data_status: Literal["available", "partial", "unavailable"]
    confirmed_market_signals: list[str]
    momentum_sentiment: str
    volume_sentiment: str
    crowding_risk: str
    missing_sentiment_data: list[str]
    confidence: int = Field(ge=0, le=100)
    summary: str
    market_phase: Literal["ice", "repair", "warming", "acceleration", "climax", "divergence", "retreat", "unavailable"] = "unavailable"
    sentiment_view: View = "unavailable"
    crowding_level: Literal["low", "medium", "high", "very_high", "unavailable"] = "unavailable"
    breadth_status: str = "unavailable"
    leader_status: str = "unavailable"
    sector_strength_status: str = "unavailable"
    evidence_quality: EvidenceQuality = "insufficient"
    confidence_cap: int | None = Field(default=None, ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_within_cap(self):
        if self.confidence_cap is not None and self.confidence > self.confidence_cap:
            raise ValueError("confidence_exceeds_confidence_cap")
        return self


class RiskReport(StrictReport):
    technical_risks: list[str]
    data_quality_risks: list[str]
    positioning_risks: list[str]
    bull_case_challenges: list[str]
    invalid_assumptions: list[str]
    missing_information: list[str]
    risk_level: RiskLevel
    confidence: int = Field(ge=0, le=100)
    summary: str
    primary_risks: list[str] = Field(default_factory=list)
    secondary_risks: list[str] = Field(default_factory=list)
    volatility_risk: str = "unavailable"
    liquidity_risk: str = "unavailable"
    trend_break_risk: str = "unavailable"
    crowding_risk: str = "unavailable"
    event_risk: str = "unavailable"
    data_risk: str = "unavailable"
    protective_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(default_factory=list)
    max_confidence_allowed: int | None = Field(default=None, ge=0, le=100)
    missing_evidence: list[str] = Field(default_factory=list)
    evidence_quality: EvidenceQuality = "insufficient"
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_within_cap(self):
        if self.max_confidence_allowed is not None and self.confidence > self.max_confidence_allowed:
            raise ValueError("confidence_exceeds_max_confidence_allowed")
        return self


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
    overall_view: View = "unavailable"
    short_term_view: View = "unavailable"
    mid_term_view: View = "unavailable"
    trend_state: TrendState = "unavailable"
    risk_level: RiskLevel = "unavailable"
    confidence_cap: int | None = Field(default=None, ge=0, le=100)
    evidence_quality: EvidenceQuality = "insufficient"
    data_completeness: DataCompleteness = "insufficient"
    key_drivers: list[str] = Field(default_factory=list)
    key_conflicts: list[KeyConflict] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    summary: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_within_cap(self):
        if self.confidence_cap is not None and self.confidence > self.confidence_cap:
            raise ValueError("confidence_exceeds_confidence_cap")
        return self
