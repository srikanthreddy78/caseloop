"""Structured-output schemas shared by every LLM provider. Strict-schema friendly: no dicts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Sentiment = Literal["angry", "frustrated", "neutral", "positive"]
Urgency = Literal["low", "normal", "high"]
CategoryLiteral = Literal["refund", "return", "damaged", "missing", "billing", "question", "other"]


class RequirementSpec(BaseModel):
    key: str = Field(
        description="snake_case identifier, e.g. order_number, photos, shipping_address"
    )
    label: str = Field(description="Short human label shown in the dashboard")
    why_needed: str = Field(description="One sentence: why resolving this case needs it")


class ExtractedValue(BaseModel):
    key: str
    value: str


class CaseAssessment(BaseModel):
    category: CategoryLiteral
    subject: str = Field(description="One-line subject, max 80 chars")
    summary: str = Field(
        description="Two or three sentences describing the issue and what the customer wants"
    )
    sentiment: Sentiment
    urgency: Urgency
    remedy_requested: str = Field(description="What the customer is asking for, or 'unspecified'")
    required: list[RequirementSpec] = Field(
        description="Everything the business must collect before it can resolve this case"
    )
    extracted: list[ExtractedValue] = Field(
        description="Values already present in the conversation, keyed by requirement key"
    )


class Theme(BaseModel):
    name: str
    share_pct: int = Field(description="Approximate share of cases in this theme, 0-100")
    example_case_ids: list[str]
    root_causes: list[str]
    suggested_fixes: list[str]


class InsightsReport(BaseModel):
    headline: str
    themes: list[Theme]
    sentiment_trend: str
    emerging_issues: list[str]
    top_escalation_reasons: list[str]
