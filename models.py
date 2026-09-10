"""Pydantic output models, one per agent and per mode. Validation of every LLM result."""
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


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent: str
    question: str = Field(..., min_length=3)


class CeoTask(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    agent: Literal["researcher", "strategist", "copywriter", "publisher"]
    title: str
    input: dict = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class Hire(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,30}$")
    name: str
    role: str
    why: str = ""
    brief: str = ""


class CeoOut(_Out):
    campaign_name: str
    objective: str
    tasks: list[CeoTask] = Field(..., min_length=1)
    hires: list[Hire] = Field(default_factory=list)
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
    question_for_colleague: Question | None = None


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
    question_for_colleague: Question | None = None


class Review(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_index: int
    verdict: Literal["clear", "revise", "block"]
    issues: list[str] = Field(default_factory=list)
    note: str = ""


class ComplianceOut(_Out):
    reviews: list[Review] = Field(default_factory=list)
    summary: str = ""


class Palette(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bg: str = "#111111"
    fg: str = "#fafafa"
    accent: str = "#ff5a1f"


def _max_words(v: str, n: int, what: str) -> str:
    v = " ".join(str(v or "").split())
    if len(v.split()) > n:
        raise ValueError(f"{what} must be at most {n} words, got {len(v.split())}: {v!r}")
    return v


class AssetSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content_index: int
    headline: str
    subline: str = ""
    photo_query: str = ""
    treatment: Literal["dark", "light"] = "dark"

    @field_validator("headline")
    @classmethod
    def _head(cls, v: str) -> str:
        return _max_words(v, 6, "headline")  # hard cap; the runtime retries once with this message

    @field_validator("subline")
    @classmethod
    def _sub(cls, v: str) -> str:
        return _max_words(v, 12, "subline")
    palette: Palette = Field(default_factory=Palette)
    layout: Literal["photo", "split", "frame", "stacked", "badge"] = "photo"
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


class Scene(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(..., min_length=1)
    subtext: str = ""
    photo_query: str = ""
    treatment: Literal["dark", "light"] = "dark"
    bg: str = "#0b0d12"
    fg: str = "#ffffff"
    accent: str = "#c6ff4a"
    seconds: float = 3.0
    style: Literal["punch", "calm", "split"] = "punch"

    @field_validator("seconds")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(1.5, min(5.0, float(v)))


class MotionOut(_Out):
    title: str
    scenes: list[Scene] = Field(..., min_length=2, max_length=8)
    caption: str = ""


class Allocation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channel: Literal["instagram", "linkedin", "x"]
    share_pct: int = 0
    daily_eur: float = 0.0
    objective: str = ""


class PaidMediaOut(_Out):
    allocation: list[Allocation] = Field(..., min_length=1)
    expected_cpa_eur: float = 0.0
    rationale: str = ""


class Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    comment_id: str
    reply: str


class Sentiment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    positive: int = 0
    neutral: int = 0
    negative: int = 0


class CommunityOut(_Out):
    replies: list[Reply] = Field(default_factory=list)
    sentiment: Sentiment = Field(default_factory=Sentiment)
    themes: list[str] = Field(default_factory=list)
    escalations: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    why: str = ""
    target_agent: Literal["strategist", "copywriter", "paid_media"] = "strategist"
    directive: Literal["new_posts", "drop_channel", "boost_channel", "re_research", "hold"] = "new_posts"
    channel: Literal["instagram", "linkedin", "x"] | None = None
    num_posts: int | None = None

    @field_validator("num_posts")
    @classmethod
    def _cap(cls, v):
        return None if v is None else max(1, min(4, int(v)))


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


class SpecialistOut(_Out):
    deliverable: str
    notes_for_copywriter: list[str] = Field(default_factory=list)


class AnswerOut(_Out):
    answer: str


class StandupLine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent: str
    line: str


class StandupOut(_Out):
    updates: list[StandupLine] = Field(default_factory=list)


class BoardReportOut(_Out):
    headline: str
    shipped: list[str] = Field(default_factory=list)
    learned: list[str] = Field(default_factory=list)
    spend_line: str = ""
    next: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ChatBrief(BaseModel):
    model_config = ConfigDict(extra="ignore")
    product_name: str = Field(..., min_length=1)
    one_liner: str = ""
    description: str = ""
    audience: str = ""
    goals: str = ""
    tone: str = ""
    budget_eur: float = 500
    channels: list[str] = Field(default_factory=lambda: ["instagram", "linkedin", "x"])


class ChatAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["none", "start_brief", "simulate_day", "pause", "resume", "message_team"] = "none"
    brief: ChatBrief | None = None
    agent: str | None = None
    note: str | None = None


class ChatOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reply: str = Field(..., min_length=1)
    actions: list[ChatAction] = Field(default_factory=list)


MODELS: dict[str, type[_Out]] = {
    "ceo": CeoOut, "researcher": ResearcherOut, "strategist": StrategistOut, "copywriter": CopywriterOut,
    "compliance": ComplianceOut, "designer": DesignerOut, "publisher": PublisherOut, "motion": MotionOut,
    "paid_media": PaidMediaOut, "community": CommunityOut, "analyst": AnalystOut, "cfo": CfoOut,
}
MODE_MODELS: dict[str, type[_Out]] = {
    "answer": AnswerOut, "standup": StandupOut, "board_report": BoardReportOut, "revise": CopywriterOut,
}


def validate(agent_key: str, data: dict, mode: str | None = None) -> dict:
    """Validate and normalise; raises pydantic.ValidationError with a readable message."""
    if mode:
        model = MODE_MODELS[mode]
    else:
        model = MODELS.get(agent_key, SpecialistOut)
    return model.model_validate(data).model_dump()
