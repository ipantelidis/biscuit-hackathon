"""Pydantic output models, one per agent. Validation of every LLM result."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_to_team: str = Field(..., min_length=1)

    @field_validator("message_to_team")
    @classmethod
    def _clip(cls, v: str) -> str:
        return v.strip()[:280]


class CeoTask(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    agent: Literal["researcher", "strategist", "copywriter", "publisher"]
    title: str
    input: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class CeoOut(_Out):
    campaign_name: str
    objective: str
    tasks: list[CeoTask] = Field(..., min_length=1)
    escalations: list[str] = Field(default_factory=list)


class Competitor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    positioning: str = ""
    weakness: str = ""


class Persona(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    who: str = ""
    pain: str = ""
    hook: str = ""


class ResearcherOut(_Out):
    market_summary: str
    competitors: list[Competitor] = Field(default_factory=list)
    personas: list[Persona] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"


class ChannelPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channel: Literal["instagram", "linkedin", "x", "landing"]
    role: str = ""
    cadence: str = ""


class WeekPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    week: int
    focus: str = ""
    posts: int = 0


class StrategistOut(_Out):
    positioning: str
    key_messages: list[str] = Field(default_factory=list)
    channels: list[ChannelPlan] = Field(default_factory=list)
    two_week_plan: list[WeekPlan] = Field(default_factory=list)
    success_metric: str = ""
    changes_from_previous_round: str | None = ""


class ContentItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channel: Literal["instagram", "linkedin", "x"]
    headline: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    cta: str = ""
    hashtags: list[str] = Field(default_factory=list)
    rationale: str = ""
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("headline")
    @classmethod
    def _clip(cls, v: str) -> str:
        return v.strip()[:80]


class CopywriterOut(_Out):
    items: list[ContentItem] = Field(..., min_length=1)


class Palette(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bg: str = "#111111"
    fg: str = "#fafafa"
    accent: str = "#ff5a1f"


class AssetSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_index: int
    headline: str
    subline: str = ""
    palette: Palette = Field(default_factory=Palette)
    layout: Literal["stacked", "split", "badge"] = "stacked"
    glyph: str = "*"
    alt_text: str = ""


class DesignerOut(_Out):
    assets: list[AssetSpec] = Field(..., min_length=1)


class ScheduleSlot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_index: int
    publish_slot: str = ""
    channel: str = ""


class PublisherOut(_Out):
    campaign_headline: str
    campaign_intro: str = ""
    schedule: list[ScheduleSlot] = Field(default_factory=list)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    why: str = ""
    target_agent: Literal["strategist", "copywriter"] = "strategist"


class AnalystOut(_Out):
    report: str
    winner_content_id: str = ""
    loser_content_id: str = ""
    findings: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class CfoOut(_Out):
    memo: str
    burn_assessment: Literal["ok", "warning", "critical"] = "ok"
    action: Literal["continue", "pause", "reduce_scope"] = "continue"


MODELS: dict[str, type[_Out]] = {
    "ceo": CeoOut,
    "researcher": ResearcherOut,
    "strategist": StrategistOut,
    "copywriter": CopywriterOut,
    "designer": DesignerOut,
    "publisher": PublisherOut,
    "analyst": AnalystOut,
    "cfo": CfoOut,
}


def validate(agent_key: str, data: dict) -> dict:
    """Validate and normalise; raises pydantic.ValidationError with a readable message."""
    return MODELS[agent_key].model_validate(data).model_dump()
