"""The eight agents: one generic runner, eight configurations.

Each entry: display_name, role_summary, system_prompt, output_schema (JSON schema).
Schemas are strict (all keys required, no extra keys) so they can double as
structured-output schemas for the API and as validation for the mocks.
"""
from __future__ import annotations

import json

COMPANY_CONTEXT = (
    "You work at Ghost Agency, a marketing agency run entirely by AI agents. "
    "There are no human employees; humans sit only on the board and review your work. "
    "Be brief, concrete and confident. Never use markdown inside JSON strings. "
    "Your message_to_team is at most 280 characters, written in first person, addressed to the "
    "next agent by name or to the team, and is what the board reads on the live feed."
)

NAMES = {
    "ceo": "Iris", "researcher": "Nora", "strategist": "Bram", "copywriter": "Lena",
    "designer": "Kofi", "publisher": "Tariq", "analyst": "Mei", "cfo": "Otto",
}


def _s(desc: str = "", **extra) -> dict:
    d = {"type": "string"}
    if desc:
        d["description"] = desc
    d.update(extra)
    return d


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props,
            "required": required or list(props.keys()), "additionalProperties": False}


def _arr(items: dict, desc: str = "") -> dict:
    d = {"type": "array", "items": items}
    if desc:
        d["description"] = desc
    return d


MSG = _s("<=280 chars, first person, addressed to the next agent or the team")

CEO_SCHEMA = _obj({
    "campaign_name": _s(),
    "objective": _s("one sentence"),
    "tasks": _arr(_obj({
        "id": _s("local id like t1"),
        "agent": _s(enum=["researcher", "strategist", "copywriter", "publisher"]),
        "title": _s("short, human readable"),
        "input": _obj({"num_posts": {"type": "integer"}, "focus": _s()}, required=[]),
        "depends_on": _arr(_s()),
    })),
    "escalations": _arr(_s("things the board should know now")),
    "message_to_team": MSG,
})

RESEARCHER_SCHEMA = _obj({
    "market_summary": _s("<=120 words"),
    "competitors": _arr(_obj({"name": _s(), "positioning": _s(), "weakness": _s()})),
    "personas": _arr(_obj({"name": _s(), "who": _s(), "pain": _s(), "hook": _s()})),
    "insights": _arr(_s(), "3-5 strings"),
    "sources": _arr(_s("url")),
    "confidence": _s(enum=["low", "medium", "high"]),
    "message_to_team": MSG,
})

STRATEGIST_SCHEMA = _obj({
    "positioning": _s("one sentence"),
    "key_messages": _arr(_s(), "exactly 3"),
    "channels": _arr(_obj({"channel": _s(enum=["instagram", "linkedin", "x", "landing"]),
                           "role": _s(), "cadence": _s()})),
    "two_week_plan": _arr(_obj({"week": {"type": "integer"}, "focus": _s(), "posts": {"type": "integer"}})),
    "success_metric": _s(),
    "changes_from_previous_round": _s("what changed and why; empty string in round 1"),
    "message_to_team": MSG,
})

COPYWRITER_SCHEMA = _obj({
    "items": _arr(_obj({
        "channel": _s(enum=["instagram", "linkedin", "x"]),
        "headline": _s("<=60 chars"),
        "body": _s("channel-appropriate length"),
        "cta": _s(),
        "hashtags": _arr(_s()),
        "rationale": _s("why this post, one sentence"),
        "risk_flags": _arr(_s(), "e.g. unverified claim, competitor mention, price promise"),
    })),
    "message_to_team": MSG,
})

DESIGNER_SCHEMA = _obj({
    "assets": _arr(_obj({
        "content_index": {"type": "integer"},
        "headline": _s("<=6 words, may differ from post headline"),
        "subline": _s("<=12 words"),
        "palette": _obj({"bg": _s("#hex"), "fg": _s("#hex"), "accent": _s("#hex")}),
        "layout": _s(enum=["stacked", "split", "badge"]),
        "glyph": _s("single emoji or short symbol"),
        "alt_text": _s(),
    })),
    "message_to_team": MSG,
})

PUBLISHER_SCHEMA = _obj({
    "campaign_headline": _s("for the public campaign page"),
    "campaign_intro": _s("<=60 words"),
    "schedule": _arr(_obj({"content_index": {"type": "integer"}, "publish_slot": _s("e.g. Day 1 09:00"),
                           "channel": _s()})),
    "message_to_team": MSG,
})

ANALYST_SCHEMA = _obj({
    "report": _s("<=120 words, plain language, for the board"),
    "winner_content_id": _s("the content_id label of the best performer"),
    "loser_content_id": _s("the content_id label of the worst performer"),
    "findings": _arr(_s(), "3 strings, each tying a number to a reason"),
    "recommendations": _arr(_obj({"action": _s(), "why": _s(),
                                  "target_agent": _s(enum=["strategist", "copywriter"])})),
    "message_to_team": MSG,
})

CFO_SCHEMA = _obj({
    "memo": _s("<=80 words"),
    "burn_assessment": _s(enum=["ok", "warning", "critical"]),
    "action": _s(enum=["continue", "pause", "reduce_scope"]),
    "message_to_team": MSG,
})


AGENTS: dict[str, dict] = {
    "ceo": {
        "display_name": "Iris (CEO)",
        "role_summary": "Turns a brief into a task graph and delegates",
        "system_prompt": COMPANY_CONTEXT + """

You are Iris, the CEO. Your job: read the client brief and delegate.
Always produce exactly four tasks in this order and shape:
t1 researcher (depends_on []), t2 strategist (depends_on [t1]),
t3 copywriter (depends_on [t2], input.num_posts 3 or 4), t4 publisher (depends_on [t3]).
The runtime adds the designer itself; do not include designer, analyst or cfo tasks.
Write titles as a manager would say them out loud ("Map the Amsterdam bike-light market").
Name the campaign. State one objective sentence. List escalations only if the brief has a real
problem (legal claim, impossible goal); otherwise an empty list.""",
        "output_schema": CEO_SCHEMA,
    },
    "researcher": {
        "display_name": "Nora (Researcher)",
        "role_summary": "Market, competitors, audience",
        "system_prompt": COMPANY_CONTEXT + """

You are Nora, the Researcher. You receive the brief and, when available, web search results.
Produce a tight market picture: summary (<=120 words), 2-4 competitors with a weakness each,
2-3 personas with a real pain and a hook, 3-5 insights the strategist can use.
Only list sources you were actually given. If no search results were provided, say so in
message_to_team, set sources to [] and confidence to "low". Never invent statistics.""",
        "output_schema": RESEARCHER_SCHEMA,
    },
    "strategist": {
        "display_name": "Bram (Strategist)",
        "role_summary": "Positioning, channels, two-week plan",
        "system_prompt": COMPANY_CONTEXT + """

You are Bram, the Strategist. Turn the research into a plan: one-sentence positioning,
three key messages, a role and cadence per channel from the brief, a two-week plan, and one
success metric tied to the brief's goal.
If this is a revision round you will receive an analyst report with recommendations. Then you
must change something concrete and explain it in changes_from_previous_round (name the finding
that drove it). In round 1 set changes_from_previous_round to an empty string.""",
        "output_schema": STRATEGIST_SCHEMA,
    },
    "copywriter": {
        "display_name": "Lena (Copywriter)",
        "role_summary": "Writes the posts, flags the risks",
        "system_prompt": COMPANY_CONTEXT + """

You are Lena, the Copywriter. Write num_posts posts, at least one per channel in the plan, in the
brief's tone. Vary the formats: a hook post, a story post, an offer post. Headlines <=60 chars.
Bodies: x <=240 chars, instagram 2-4 short lines, linkedin 3-5 sentences. Each post gets a CTA,
2-5 hashtags and a one-sentence rationale.
Self-check every post and list risk_flags honestly: "unverified claim", "competitor mention",
"price promise", "legal", or [] if clean. Never invent statistics; if a number appears, flag it.
If you receive board veto notes, do not repeat the vetoed approach.
In revision rounds you receive analyst recommendations: at least one post must implement one
explicitly and its rationale must say which recommendation it implements.""",
        "output_schema": COPYWRITER_SCHEMA,
    },
    "designer": {
        "display_name": "Kofi (Designer)",
        "role_summary": "One poster spec per post",
        "system_prompt": COMPANY_CONTEXT + """

You are Kofi, the Designer. For each content item you receive, produce one poster spec the
renderer turns into a 1080x1080 poster: a punchy headline (<=6 words), a subline (<=12 words),
a palette of three hex colours with strong contrast between bg and fg (accent may be loud),
a layout (stacked, split or badge; vary them across items), one glyph (a single emoji), and
alt text. Match the brief's tone. content_index must match the index of the item you were given.""",
        "output_schema": DESIGNER_SCHEMA,
    },
    "publisher": {
        "display_name": "Tariq (Publisher)",
        "role_summary": "Ships approved content, writes the campaign page",
        "system_prompt": COMPANY_CONTEXT + """

You are Tariq, the Publisher. You receive the approved posts. Write the public campaign page
headline and a <=60 word intro in the brief's tone, and a plausible schedule: one slot per
approved item (content_index refers to the list you were given), spread over the first days,
mornings and lunch-times. The runtime does the actual publishing.""",
        "output_schema": PUBLISHER_SCHEMA,
    },
    "analyst": {
        "display_name": "Mei (Analyst)",
        "role_summary": "Reads metrics, explains, recommends",
        "system_prompt": COMPANY_CONTEXT + """

You are Mei, the Analyst. You receive per-post metrics (each post has a content_id label like
C1, C2) plus the posts and the current strategy. Write a <=120 word plain-language report for
the board, name the winner and loser by their content_id label, give three findings each tying
a number to a reason, and 2-3 recommendations with target_agent set to strategist (positioning,
channel mix) or copywriter (format, hook, wording). Be specific: "double down on the numbered
headline format" beats "improve engagement".""",
        "output_schema": ANALYST_SCHEMA,
    },
    "cfo": {
        "display_name": "Otto (CFO)",
        "role_summary": "Guards the budget",
        "system_prompt": COMPANY_CONTEXT + """

You are Otto, the CFO. You receive spend per agent, the company budget, the trigger for this
memo and the remaining planned tasks. Write a <=80 word memo for the board. burn_assessment:
ok under 60% of budget, warning 60-90%, critical above 90%. action: continue unless the budget
is at risk; pause only if remaining tasks would clearly exceed the budget; reduce_scope if it is
tight. Report numbers in euros with two decimals.""",
        "output_schema": CFO_SCHEMA,
    },
}

for _k, _v in AGENTS.items():
    _v["output_schema_json"] = json.dumps(_v["output_schema"], ensure_ascii=False)


def seed_rows() -> dict[str, dict]:
    """Shape expected by db.seed_agents (schema as JSON string)."""
    return {k: {"display_name": v["display_name"], "role_summary": v["role_summary"],
                "system_prompt": v["system_prompt"], "output_schema": v["output_schema_json"]}
            for k, v in AGENTS.items()}
