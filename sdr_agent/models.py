"""Typed data that flows through the pipeline.

Prospect -> ResearchNotes -> AccountBrief -> EmailDraft -> Grade -> SDRResult
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Prospect(BaseModel):
    """One person at one account. This is the pipeline's input row."""

    company_domain: str
    first_name: str
    last_name: str = ""
    title: str
    email: str | None = None
    linkedin_url: str | None = None


class Source(BaseModel):
    title: str
    url: str


class ResearchNotes(BaseModel):
    """Free-text research plus the URLs it came from."""

    notes: str
    sources: list[Source] = Field(default_factory=list)


class Trigger(BaseModel):
    """A 'why now' event: something that changed recently at the account."""

    kind: Literal[
        "funding", "hiring", "leadership_change", "product_launch",
        "expansion", "tech_change", "partnership", "other",
    ]
    summary: str
    date: str = Field(description="ISO date or 'unknown'")
    source_url: str = Field(description="URL from the research sources that supports this trigger")


class AccountBrief(BaseModel):
    """Structured account research the writer is allowed to rely on."""

    company_name: str
    one_liner: str
    industry: str
    employee_range: str
    icp_fit_score: int = Field(description="0-100 fit against the seller's ICP")
    icp_fit_reason: str
    likely_pains: list[str]
    triggers: list[Trigger]
    persona_angle: str = Field(description="What this person's role likely cares about")
    personalization_hooks: list[str]


class EmailDraft(BaseModel):
    subject: str
    body: str
    angle: str = Field(description="One sentence: the core argument of the email")
    trigger_used: str = Field(description="Which trigger the opener references, or 'none'")


class RubricScore(BaseModel):
    criterion: Literal[
        "personalization", "relevance", "grounded", "clarity", "cta", "brevity",
    ]
    score: int = Field(description="1-5")
    note: str


class Grade(BaseModel):
    scores: list[RubricScore]
    overall: float = Field(description="Mean of the criterion scores, 1-5")
    passed: bool
    unsupported_claims: list[str] = Field(
        description="Claims in the email not supported by the account brief"
    )
    revision_instructions: str


class Attempt(BaseModel):
    draft: EmailDraft
    grade: Grade


class SDRResult(BaseModel):
    prospect: Prospect
    research: ResearchNotes
    brief: AccountBrief
    attempts: list[Attempt]

    @property
    def final(self) -> Attempt:
        return self.attempts[-1]

    @property
    def approved(self) -> bool:
        return self.final.grade.passed
