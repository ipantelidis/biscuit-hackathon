"""The nervous system: run_agent, orchestrator tick, post-processing hooks, budget guard."""
from __future__ import annotations

import json
import re
import logging
import os
import threading
import time
import traceback
from pathlib import Path

from pydantic import ValidationError

import db
import models
import tools
from agents import AGENTS, CHAT_PROMPT, CHAT_SCHEMA, MODES, SPECIALIST_SCHEMA, specialist_prompt

log = logging.getLogger("ghost.runtime")
ROOT = Path(__file__).parent
MEDIA_DIR = ROOT / "media"

TO_AGENT = {"ceo": "all", "researcher": "strategist", "seo": "copywriter", "strategist": "copywriter", "copywriter": "compliance",
            "compliance": "board", "designer": "board", "publisher": "all", "motion": "board",
            "paid_media": "board", "community": "analyst", "analyst": "strategist", "cfo": "board"}
KIND = {"ceo": "status", "researcher": "handoff", "seo": "handoff", "strategist": "handoff", "copywriter": "handoff",
        "compliance": "handoff", "designer": "handoff", "publisher": "status", "motion": "handoff",
        "paid_media": "report", "community": "report", "analyst": "report", "cfo": "report"}
HIRE_COLORS = ["#ffd166", "#06d6a0", "#ef476f", "#8ecae6", "#f4a261"]

BRIEF_FIELDS = ("product_name", "one_liner", "description", "audience", "goals",
                "budget_eur", "tone", "channels")

DECIDED = ("approved", "vetoed", "published", "blocked")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ bus / company

def post_message(brief_id: str | None, from_agent: str, to_agent: str, kind: str, body: str,
                 task_id: str | None = None) -> str:
    return db.insert("messages", {"brief_id": brief_id, "from_agent": from_agent, "to_agent": to_agent,
                                  "kind": kind, "body": (body or "").strip()[:600], "task_id": task_id})


def company() -> dict:
    return db.get("company", "company")


def current_brief_id() -> str | None:
    rows = db.query("briefs", order="created_at DESC", limit=1)
    return rows[0]["id"] if rows else None


def pause_company(reason: str, by: str = "board") -> None:
    db.update("company", "company", {"paused": 1})
    db.execute("UPDATE agents SET status='paused', updated_at=? WHERE status IN ('idle','working')", [db.now()])
    post_message(current_brief_id(), by, "all", "alert", reason)


def resume_company(by: str = "board") -> None:
    db.update("company", "company", {"paused": 0})
    db.execute("UPDATE agents SET status='idle', updated_at=? WHERE status='paused'", [db.now()])
    post_message(current_brief_id(), by, "all", "status", "Board resumed the company. Back to work.")


def agent_name(key: str) -> str:
    a = db.get("agents", key, id_col="key")
    return a["display_name"].split(" (")[0] if a else key


# ------------------------------------------------------------------ context builders

def _brief_public(brief: dict) -> dict:
    return {k: brief.get(k) for k in BRIEF_FIELDS}


def _content_for_round(brief_id: str, rnd: int, statuses: tuple[str, ...] | None = None) -> list[dict]:
    where, params = "brief_id = ? AND round = ?", [brief_id, rnd]
    if statuses:
        where += " AND status IN (%s)" % ",".join("?" * len(statuses))
        params += list(statuses)
    return db.query("content", where, params)


def _content_view(c: dict, idx: int | None = None) -> dict:
    d = {"channel": c["channel"], "headline": c["headline"], "body": c["body"], "cta": c["cta"],
         "hashtags": c["hashtags"], "rationale": c["rationale"], "risk_flags": c["risk_flags"]}
    if idx is not None:
        d = {"content_index": idx, **d}
    return d


def _latest_output(brief_id: str, agent_key: str) -> dict | None:
    rows = db.query("tasks", "brief_id = ? AND agent_key = ? AND status = 'done' AND json_extract(input,'$.mode') IS NULL",
                    [brief_id, agent_key], order="created_at DESC", limit=1)
    return rows[0]["output"] if rows else None


DESIGN_WORDS = ("align", "overflow", "cut off", "clip", "colour", "color", "font", "poster", "layout", "glyph",
                "palette", "image", "contrast", "unreadable", "visual", "picture")


def classify_note(note: str) -> str:
    n = (note or "").lower()
    return "designer" if any(w in n for w in DESIGN_WORDS) else "copywriter"


def post_performance(brief_id: str) -> list[dict]:
    """Every content row with its live days, daily rows and ctr_per_day (mean of daily CTRs)."""
    out = []
    for c in db.query("content", "brief_id = ?", [brief_id], order="round ASC, created_at ASC"):
        ms = db.query("metrics", "content_id = ?", [c["id"]], order="day ASC")
        per_day = [{"day": m["day"], "impressions": m["impressions"], "clicks": m["clicks"], "signups": m["signups"],
                    "ctr": m["ctr"]} for m in ms]
        ctr_per_day = round(sum(m["ctr"] for m in ms) / len(ms), 4) if ms else None
        out.append({"id": c["id"], "round": c["round"], "channel": c["channel"], "headline": c["headline"],
                    "body": (c["body"] or "")[:200], "rationale": c["rationale"], "status": c["status"],
                    "risk_flags": c["risk_flags"], "days_live": len(ms), "per_day": per_day,
                    "ctr_per_day": ctr_per_day, "signups": sum(m["signups"] for m in ms),
                    "ad_spend_eur": round(sum(m["ad_spend_eur"] or 0 for m in ms), 2)})
    return out


def published_ranking(brief_id: str) -> list[dict]:
    """Published posts labelled C1.. in publish order, plus the ranking by ctr_per_day."""
    pub = [p for p in post_performance(brief_id) if p["status"] == "published"]
    pub.sort(key=lambda p: (db.get("content", p["id"]).get("published_at") or "", p["round"]))
    for i, p in enumerate(pub):
        p["label"] = f"C{i + 1}"
    return pub


def active_channels(brief: dict) -> list[str]:
    return list(brief.get("active_channels") or brief.get("channels") or ["instagram", "linkedin", "x"])


def _metrics_rows(brief_id: str) -> list[dict]:
    rows = []
    for i, c in enumerate(db.query("content", "brief_id = ? AND status = 'published'", [brief_id])):
        ms = db.query("metrics", "content_id = ?", [c["id"]])
        agg = {k: sum(m[k] for m in ms) for k in ("impressions", "clicks", "likes", "shares", "signups",
                                                    "paid_impressions")}
        agg["ad_spend_eur"] = round(sum(m["ad_spend_eur"] or 0 for m in ms), 2)
        agg["ctr"] = round(agg["clicks"] / agg["impressions"], 4) if agg["impressions"] else 0.0
        agg["cost_per_signup_eur"] = round(agg["ad_spend_eur"] / agg["signups"], 2) if agg["signups"] else None
        rows.append({"content_id": f"C{i + 1}", "round": c["round"], "channel": c["channel"],
                     "headline": c["headline"], "body": c["body"][:200], "metrics": agg})
    return rows


def ctx_researcher(task: dict, brief: dict) -> str:
    product = brief["product_name"]
    category = (brief.get("one_liner") or "")[:80]
    focus = task["input"].get("focus")
    if focus:
        queries = [f"{focus}", f"{product} {focus}", f"{focus} {brief.get('audience', '')[:40]}"]
    else:
        queries = [f"{category} competitors", f"{product} target audience {brief.get('audience', '')[:60]}",
                   f"{category} market Netherlands"]
    results, seen = [], set()
    for q in queries:
        for r in tools.web_search(q, k=3):
            if r.get("url") and r["url"] not in seen:
                seen.add(r["url"])
                results.append({"query": q, **r})
    if not results:
        return ("## Web search\nNo search results available. Work from your own knowledge, "
                "set sources to [] and confidence to \"low\", and say so in message_to_team.")
    return "## Web search results (the only sources you may cite)\n" + _j(results[:8])


def ctx_copywriter(task: dict, brief: dict) -> str:
    parts = []
    if task["input"].get("mode") == "revise":
        items = []
        for i, cid in enumerate(task["input"].get("revise_content_ids", [])):
            c = db.get("content", cid)
            if c:
                items.append({**_content_view(c, i), "compliance_note": c.get("compliance_note")})
        parts.append("## Posts to rewrite (same order, one item each)\n" + _j(items))
    notes = veto_notes(brief["id"], ("copywriter", "both"), since_round=task["round"] - 1)
    if notes:
        parts.append("## Board veto notes on earlier posts (do not repeat these approaches)\n" + _j(notes))
    seo = _latest_output(brief["id"], "seo")
    if seo:
        parts.append("## Search and wording rules from Yara (SEO). Use the keywords naturally; follow the rules.\n"
                     + _j({k: seo.get(k) for k in ("primary_keywords", "long_tail", "search_phrases_by_channel",
                                                    "hashtags_by_channel", "wording_rules")}))
    specialists = db.query("tasks", "brief_id = ? AND status = 'done' AND json_extract(input,'$.specialist') = 1",
                           [brief["id"]])
    for sp in specialists:
        parts.append(f"## Notes from {agent_name(sp['agent_key'])} (hired specialist)\n" + _j(sp["output"]))
    chans = active_channels(brief)
    parts.append("## Active channels (write only for these)\n" + _j(chans)
                 + (f"\nDropped by the analyst: {_j(sorted(set(brief.get('channels') or []) - set(chans)))}"
                    if set(brief.get("channels") or []) - set(chans) else ""))
    if task["input"].get("channel_focus"):
        parts.append(f"Channel focus this round: {task['input']['channel_focus']} (it converts best; give it one extra post).")
    if task["round"] > 1:
        prev = [p for p in post_performance(brief["id"]) if p["round"] < task["round"]]
        full = [p for p in prev if p["round"] >= task["round"] - 3]
        older = [p for p in prev if p["round"] < task["round"] - 3]
        if full:
            parts.append("## Every post the company already ran, with numbers. Do not reuse a headline, "
                         "an opening line or a hook family from this list.\n"
                         + _j([{k: p[k] for k in ("round", "channel", "headline", "body", "rationale", "status",
                                                  "days_live", "ctr_per_day", "signups")} for p in full]))
        if older:
            parts.append("## Older posts (headlines only; do not reuse)\n"
                         + _j([{"round": p["round"], "channel": p["channel"], "headline": p["headline"]} for p in older]))
    return "\n\n".join(parts)


def veto_notes(brief_id: str, targets: tuple[str, ...], since_round: int) -> list[dict]:
    out = []
    for a in db.query("approvals", "decision = 'vetoed' AND note IS NOT NULL AND note != ''", order="created_at ASC"):
        c = db.get("content", a["content_id"])
        if not c or c["brief_id"] != brief_id:
            continue
        if (a.get("target") or "copywriter") not in targets or int(a.get("round") or c["round"]) < since_round:
            continue
        out.append({"round": c["round"], "channel": c["channel"], "headline": c["headline"], "board_note": a["note"]})
    return out


def ctx_strategist(task: dict, brief: dict) -> str:
    if not task["input"].get("revision"):
        return ""
    parts = []
    log_rows = [{"round": t["round"], "changes_from_previous_round": (t["output"] or {}).get("changes_from_previous_round")}
                for t in db.query("tasks", "brief_id = ? AND agent_key = 'strategist' AND status = 'done' "
                                  "AND json_extract(input,'$.mode') IS NULL", [brief["id"]], order="round ASC")]
    parts.append("## Strategy changelog (do not repeat these changes)\n" + _j(log_rows))
    perf = [{k: p[k] for k in ("round", "channel", "headline", "status", "days_live", "ctr_per_day", "signups")}
            for p in post_performance(brief["id"])]
    parts.append("## Content performance so far\n" + _j(perf))
    parts.append("## Active channels\n" + _j(active_channels(brief)))
    return "\n\n".join(parts)


def ctx_seo(task: dict, brief: dict) -> str:
    if task["round"] <= 1:
        return ""
    perf = [{k: p[k] for k in ("round", "channel", "headline", "body", "days_live", "ctr_per_day", "signups")}
            for p in post_performance(brief["id"]) if p["status"] == "published"]
    prev = _latest_output(brief["id"], "seo")
    parts = ["## How the posts did (lowest ctr_per_day = wording to fix first)\n" + _j(sorted(perf, key=lambda p: p["ctr_per_day"] or 0))]
    if prev:
        parts.append("## Your previous keywords and rules\n" + _j({k: prev.get(k) for k in ("primary_keywords", "wording_rules")}))
    return "\n\n".join(parts)


def ctx_compliance(task: dict, brief: dict) -> str:
    items = []
    for i, cid in enumerate(task["input"].get("content_ids", [])):
        c = db.get("content", cid)
        if c:
            items.append(_content_view(c, i))
    return f"## Posts to review (use content_index)\n{_j(items)}"


def ctx_designer(task: dict, brief: dict) -> str:
    items = []
    for i, cid in enumerate(task["input"].get("content_ids", [])):
        c = db.get("content", cid)
        if c:
            item = _content_view(c, i)
            if c.get("asset_id"):
                prev = db.get("assets", c["asset_id"])
                if prev:
                    item["previous_poster_spec"] = prev["spec"]
            items.append(item)
    parts = [f"## Content items to design (use content_index)\n{_j(items)}\n\nBrief tone: {brief.get('tone')}"]
    if task["input"].get("redesign"):
        parts.append("## Redesign\nThe board vetoed the previous poster. Board note: "
                     f"{task['input'].get('note')}\nMake a clearly different poster that fixes the note; "
                     "change layout or palette, not just words.")
    notes = veto_notes(brief["id"], ("designer", "both"), since_round=task["round"] - 1)
    if notes:
        parts.append("## Board notes on earlier posters (fix these)\n" + _j(notes))
    return "\n\n".join(parts)


def ctx_publisher(task: dict, brief: dict) -> str:
    approved = _content_for_round(brief["id"], task["round"], ("approved",))
    items = [_content_view(c, i) for i, c in enumerate(approved)]
    return f"## Approved content to publish (round {task['round']}, use content_index)\n{_j(items)}"


def ctx_motion(task: dict, brief: dict) -> str:
    pub = db.query("content", "brief_id = ? AND status = 'published'", [brief["id"]])
    return (f"## Published posts\n{_j([_content_view(c) for c in pub])}\n\n"
            f"Campaign headline: {brief.get('campaign_headline')}\nIntro: {brief.get('campaign_intro')}")


def ctx_paid_media(task: dict, brief: dict) -> str:
    parts = [f"## Client budget\nbudget_eur: {brief.get('budget_eur')}\nchannels live: "
             f"{sorted({c['channel'] for c in db.query('content', 'brief_id = ? AND status = ?', [brief['id'], 'published'])})}"]
    strat = _latest_output(brief["id"], "strategist")
    if strat:
        parts.append("## Strategy\n" + _j({"positioning": strat.get("positioning"), "channels": strat.get("channels")}))
    if task["round"] > 1:
        prev = db.query("ad_plans", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
        if prev:
            parts.append("## Your previous allocation\n" + _j(prev[0]["allocation"]))
        parts.append("## Measured results per post\n" + _j(_metrics_rows(brief["id"])))
        an = _latest_output(brief["id"], "analyst")
        if an:
            parts.append("## Analyst report\n" + _j({"report": an.get("report"), "recommendations": an.get("recommendations")}))
    return "\n\n".join(parts)


def ctx_community(task: dict, brief: dict) -> str:
    day = task["input"].get("day")
    cs = db.query("comments", "brief_id = ? AND day = ? AND reply IS NULL", [brief["id"], day], order="label ASC")
    posts = {c["id"]: c for c in db.query("content", "brief_id = ? AND status = 'published'", [brief["id"]])}
    items = [{"id": c["label"], "author": c["author"], "channel": c["channel"], "text": c["text"],
              "on_post": posts.get(c["content_id"], {}).get("headline")} for c in cs]
    return f"## Today's comments (day {day}), reply to each by id\n{_j(items)}\n\nBrief tone: {brief.get('tone')}"


def ctx_analyst(task: dict, brief: dict) -> str:
    ranked = published_ranking(brief["id"])
    rows = []
    for p in ranked:
        rows.append({"content_id": p["label"], "round": p["round"], "channel": p["channel"], "headline": p["headline"],
                     "body": p["body"], "days_live": p["days_live"], "ctr_per_day": p["ctr_per_day"],
                     "signups": p["signups"], "ad_spend_eur": p["ad_spend_eur"],
                     "cost_per_signup_eur": round(p["ad_spend_eur"] / p["signups"], 2) if p["signups"] and p["ad_spend_eur"] else None,
                     "per_day": p["per_day"]})
    ranking = sorted(ranked, key=lambda p: -(p["ctr_per_day"] or 0))
    parts = [f"## Metrics through day {brief.get('day')} per published post\n{_j(rows)}",
             "## Ranking by ctr_per_day (highest first; your winner and loser must match this)\n"
             + _j([{"content_id": p["label"], "ctr_per_day": p["ctr_per_day"], "days_live": p["days_live"]} for p in ranking]),
             "## Active channels\n" + _j(active_channels(brief))]
    plan = db.query("ad_plans", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
    if plan:
        parts.append("## Ad plan in effect\n" + _j(plan[0]["allocation"]))
    comm = _latest_output(brief["id"], "community")
    if comm:
        parts.append("## Community sentiment (from Pim)\n" + _j({"sentiment": comm.get("sentiment"),
                                                                 "themes": comm.get("themes"),
                                                                 "escalations": comm.get("escalations")}))
    strat = _latest_output(brief["id"], "strategist")
    if strat:
        parts.append("## Current strategy\n" + _j(strat))
    return "\n\n".join(parts)


def ctx_cfo(task: dict, brief: dict) -> str:
    c = company()
    agents = db.query("agents", order="cost_eur DESC")
    spend = [{"agent": a["key"], "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"],
              "cost_eur": round(a["cost_eur"], 4)} for a in agents if a["cost_eur"]]
    remaining = db.query("tasks", "brief_id = ? AND status NOT IN ('done','failed')", [brief["id"]])
    return (f"## Budget\nbudget_eur: {c['budget_eur']:.2f}\nspent_eur: {c['spent_eur']:.4f}\n"
            f"trigger: {task['input'].get('trigger', 'manual')}\n\n## Spend per agent\n{_j(spend)}\n\n"
            f"## Remaining planned tasks\n{_j([{'agent': t['agent_key'], 'title': t['title']} for t in remaining])}")


def ctx_mode(task: dict, brief: dict) -> str:
    mode = task["input"]["mode"]
    if mode == "answer":
        own = _latest_output(brief["id"], task["agent_key"])
        parts = [f"## Question from {agent_name(task['input'].get('from_agent', ''))}\n{task['input'].get('question')}"]
        if own:
            parts.append("## Your earlier work on this campaign\n" + _j(own))
        return "\n\n".join(parts)
    if mode == "standup":
        done = db.query("tasks", "brief_id = ? AND status = 'done' AND json_extract(input,'$.mode') IS NULL",
                        [brief["id"]], order="created_at ASC")
        seen, work = set(), []
        for t in done:
            work.append({"agent": t["agent_key"], "name": agent_name(t["agent_key"]), "did": t["title"],
                         "said": (t["output"] or {}).get("message_to_team", "")[:160]})
            seen.add(t["agent_key"])
        return (f"## Day {brief.get('day')} standup. Agents and what they did\n{_j(work)}\n\n"
                f"## Latest metrics\n{_j(_metrics_rows(brief['id']))[:2500]}")
    if mode == "board_report":
        c = company()
        parts = [f"## Round {task['round']} summary inputs",
                 "Published posts: " + _j([{"round": x["round"], "channel": x["channel"], "headline": x["headline"]}
                                          for x in db.query("content", "brief_id = ? AND status = 'published'", [brief["id"]])]),
                 f"LLM spend so far: EUR {c['spent_eur']:.2f} of EUR {c['budget_eur']:.2f}"]
        plan = db.query("ad_plans", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
        if plan:
            parts.append("Ad plan: " + _j(plan[0]["allocation"]))
        m = _metrics_rows(brief["id"])
        if m:
            parts.append("Metrics: " + _j(m))
        an = _latest_output(brief["id"], "analyst")
        if an:
            parts.append("Analyst: " + _j({"report": an.get("report"), "recommendations": an.get("recommendations")}))
        comm = _latest_output(brief["id"], "community")
        if comm:
            parts.append("Community: " + _j({"sentiment": comm.get("sentiment"), "escalations": comm.get("escalations")}))
        vet = [x for x in db.query("content", "brief_id = ?", [brief["id"]]) if x["status"] in ("vetoed", "blocked")]
        if vet:
            parts.append("Rejected posts: " + _j([{"headline": x["headline"], "status": x["status"]} for x in vet]))
        return "\n\n".join(parts)
    if mode == "revise":
        return ctx_copywriter(task, brief)
    return ""


CONTEXT_BUILDERS = {"researcher": ctx_researcher, "seo": ctx_seo, "strategist": ctx_strategist, "copywriter": ctx_copywriter, "compliance": ctx_compliance,
                    "designer": ctx_designer, "publisher": ctx_publisher, "motion": ctx_motion,
                    "paid_media": ctx_paid_media, "community": ctx_community, "analyst": ctx_analyst,
                    "cfo": ctx_cfo}


def build_user_message(task: dict, brief: dict) -> str:
    parts = ["## Client brief\n" + _j(_brief_public(brief)),
             f"## Your task\nTitle: {task['title']}\nRound: {task['round']}\nInput: {_j(task['input'])}"]
    for dep_id in task["depends_on"] or []:
        dep = db.get("tasks", dep_id)
        if dep and dep.get("output"):
            parts.append(f"## Output of {dep['agent_key']} ({dep['title']})\n{_j(dep['output'])}")
    mode = task["input"].get("mode")
    if mode:
        extra = ctx_mode(task, brief)
    else:
        builder = CONTEXT_BUILDERS.get(task["agent_key"])
        extra = builder(task, brief) if builder else ""
    if extra:
        parts.append(extra)
    msgs = db.query("messages", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=10)
    if msgs:
        parts.append("## Last messages on the team channel\n" + "\n".join(
            f"{m['from_agent']} -> {m['to_agent']} [{m['kind']}]: {m['body']}" for m in reversed(msgs)))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ LLM call with validation

def _schema_for(agent: dict, mode: str | None) -> dict:
    if mode:
        return MODES[mode]["schema"]
    if agent["key"] in AGENTS:
        return AGENTS[agent["key"]]["output_schema"]
    return agent["output_schema"] if isinstance(agent["output_schema"], dict) else SPECIALIST_SCHEMA


def call_and_validate(agent: dict, system: str, user: str, mode: str | None, variant: str | None) -> tuple[dict, int, int]:
    agent_key = agent["key"]
    schema = _schema_for(agent, mode)
    if mode:
        system = system + "\n\n## Special assignment\n" + MODES[mode]["prompt"]
    system_full = (system + "\n\nRespond with a single JSON object matching this schema and nothing else:\n"
                   + json.dumps(schema))
    tin_total = tout_total = 0
    last_err: Exception | None = None
    prompt = user
    for attempt in range(2):
        try:
            text, tin, tout = tools.call_llm(agent_key, system_full, prompt, schema, variant)
            tin_total += tin
            tout_total += tout
            data = tools.parse_json(text)
            return models.validate(agent_key, data, mode), tin_total, tout_total
        except tools.LLMError as e:
            last_err = e
            log.warning("LLM error for %s (attempt %d): %s", agent_key, attempt + 1, e)
            time.sleep(2 if attempt == 0 and not tools.env_flag("MOCK_LLM") else 0)
        except (ValueError, ValidationError) as e:
            last_err = e
            log.warning("invalid output from %s (attempt %d): %s", agent_key, attempt + 1, e)
            prompt = (user + "\n\n## Your previous response was invalid\n"
                      + str(e)[:800] + "\nRespond again with a single valid JSON object and nothing else.")
    raise RuntimeError(str(last_err))


# ------------------------------------------------------------------ spend

def record_spend(agent_key: str, task_id: str, tokens_in: int, tokens_out: int) -> float:
    pin = float(os.environ.get("PRICE_IN_PER_MTOK", "2.0"))
    pout = float(os.environ.get("PRICE_OUT_PER_MTOK", "10.0"))
    cost = tokens_in / 1e6 * pin + tokens_out / 1e6 * pout
    db.insert("spend", {"agent_key": agent_key, "task_id": task_id, "tokens_in": tokens_in,
                        "tokens_out": tokens_out, "cost_eur": cost})
    db.execute("UPDATE agents SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
               "cost_eur = cost_eur + ?, updated_at = ? WHERE key = ?",
               [tokens_in, tokens_out, cost, db.now(), agent_key])
    db.execute("UPDATE company SET spent_eur = spent_eur + ?, updated_at = ? WHERE id = 'company'",
               [cost, db.now()])
    return cost


def budget_guard(brief_id: str | None) -> None:
    """Code-level guard: 80% -> CFO task once; 100% -> pause immediately."""
    c = company()
    if c["budget_eur"] <= 0:
        return
    if c["spent_eur"] >= c["budget_eur"]:
        if not c["paused"]:
            pause_company(f"Budget exhausted: EUR {c['spent_eur']:.2f} of EUR {c['budget_eur']:.2f} spent. "
                          "Company paused automatically.", by="cfo")
        return
    if c["spent_eur"] >= 0.8 * c["budget_eur"]:
        exists = db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_key='cfo' AND json_extract(input,'$.trigger')='budget_80'")
        if not exists and brief_id:
            db.insert("tasks", {"brief_id": brief_id, "agent_key": "cfo", "title": "Budget check: 80% of budget spent",
                                "input": {"trigger": "budget_80"}, "depends_on": [], "status": "ready",
                                "round": _current_round(brief_id)})
            post_message(brief_id, "cfo", "board", "alert",
                         f"Heads up: we have spent EUR {c['spent_eur']:.2f} of EUR {c['budget_eur']:.2f}. I am reviewing.")


def _current_round(brief_id: str) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(round),1) FROM tasks WHERE brief_id=?", [brief_id]) or 1)


def _add_dep(task_id: str, dep_id: str) -> None:
    t = db.get("tasks", task_id)
    if t and dep_id not in (t["depends_on"] or []):
        db.update("tasks", task_id, {"depends_on": list(t["depends_on"] or []) + [dep_id],
                                     "status": "blocked" if t["status"] in ("ready", "blocked") else t["status"]})


def _publisher_task(brief_id: str, rnd: int) -> dict | None:
    rows = db.query("tasks", "brief_id = ? AND agent_key = 'publisher' AND round = ? AND status != 'done'", [brief_id, rnd])
    return rows[0] if rows else None


# ------------------------------------------------------------------ hooks

def hook_ceo(task: dict, out: dict, brief: dict) -> None:
    id_map: dict[str, str] = {}
    created = []
    for t in out["tasks"]:
        tid = db.new_id()
        id_map[t["id"]] = tid
        created.append((tid, t))
    for tid, t in created:
        deps = [id_map[d] for d in t.get("depends_on", []) if d in id_map]
        db.insert("tasks", {"id": tid, "brief_id": brief["id"], "agent_key": t["agent"], "title": t["title"],
                            "input": t.get("input") or {}, "depends_on": deps,
                            "status": "ready" if not deps else "blocked", "round": 1})
    db.update("briefs", brief["id"], {"status": "in_progress", "campaign_name": out["campaign_name"],
                                      "objective": out["objective"]})
    by_agent = {t["agent"]: tid for tid, t in created}
    # search and wording: Yara works from Nora's research; Lena waits for her
    seo_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "seo", "title": "Find the words people search for",
                                 "input": {}, "depends_on": [by_agent["researcher"]] if "researcher" in by_agent else [],
                                 "status": "blocked" if "researcher" in by_agent else "ready", "round": 1})
    if "copywriter" in by_agent:
        _add_dep(by_agent["copywriter"], seo_id)
    # hiring: create the specialist agent and a task that feeds the copywriter
    for h in (out.get("hires") or [])[:1]:
        key = h["key"]
        if key in AGENTS:
            key = f"{key}_specialist"
        n_hired = int(db.scalar("SELECT COUNT(*) FROM agents WHERE hired = 1") or 0)
        if not db.get("agents", key, id_col="key"):
            db.insert("agents", {"key": key, "display_name": f"{h['name']} ({h['role']})", "role_summary": h["why"][:80],
                                 "system_prompt": specialist_prompt(h["name"], h["role"], h["brief"]),
                                 "output_schema": json.dumps(SPECIALIST_SCHEMA), "status": "idle", "hired": 1,
                                 "color": HIRE_COLORS[n_hired % len(HIRE_COLORS)]})
        sp_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": key, "title": f"{h['role']}: {h['brief'][:70]}",
                                    "input": {"specialist": 1, "role": h["role"]},
                                    "depends_on": [by_agent["strategist"]] if "strategist" in by_agent else [],
                                    "status": "blocked" if "strategist" in by_agent else "ready", "round": 1})
        if "copywriter" in by_agent:
            _add_dep(by_agent["copywriter"], sp_id)
        post_message(brief["id"], "ceo", "all", "status",
                     f"I hired {h['name']} as {h['role']}: {h['why']}", task["id"])
    for esc in out.get("escalations") or []:
        post_message(brief["id"], "ceo", "board", "alert", f"Escalation: {esc}", task["id"])


def hook_copywriter(task: dict, out: dict, brief: dict) -> None:
    if task["input"].get("mode") == "revise":
        ids = task["input"].get("revise_content_ids", [])
        for cid, item in zip(ids, out["items"]):
            db.update("content", cid, {"headline": item["headline"], "body": item["body"], "cta": item["cta"],
                                       "hashtags": item["hashtags"], "rationale": item["rationale"],
                                       "risk_flags": item["risk_flags"], "status": "pending_approval",
                                       "compliance_verdict": "revised"})
            db.insert("approvals", {"content_id": cid, "requested_by": "copywriter"})
        return
    content_ids = []
    chans = active_channels(brief)
    dropped = [it for it in out["items"] if it["channel"] not in chans]
    if dropped:
        post_message(brief["id"], "copywriter", "board", "alert",
                     f"Discarded {len(dropped)} post(s) on dropped channel(s) "
                     f"{', '.join(sorted({d['channel'] for d in dropped}))}: {dropped[0]['headline']}", task["id"])
    for item in [it for it in out["items"] if it["channel"] in chans]:
        cid = db.insert("content", {"brief_id": brief["id"], "task_id": task["id"], "channel": item["channel"],
                                    "headline": item["headline"], "body": item["body"], "cta": item["cta"],
                                    "hashtags": item["hashtags"], "rationale": item["rationale"],
                                    "risk_flags": item["risk_flags"], "status": "in_review",
                                    "round": task["round"]})
        content_ids.append(cid)
    if not content_ids:
        post_message(brief["id"], "copywriter", "board", "alert", "No posts survived the channel filter this round.", task["id"])
        return
    comp_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "compliance",
                                  "title": f"Review {len(content_ids)} posts before the board (round {task['round']})",
                                  "input": {"content_ids": content_ids}, "depends_on": [task["id"]],
                                  "status": "ready", "round": task["round"]})
    pub = _publisher_task(brief["id"], task["round"])
    if pub:
        _add_dep(pub["id"], comp_id)


def hook_compliance(task: dict, out: dict, brief: dict) -> None:
    content_ids = task["input"].get("content_ids", [])
    reviews = {r["content_index"]: r for r in out["reviews"]}
    to_revise, live_ids = [], []
    for i, cid in enumerate(content_ids):
        r = reviews.get(i) or {"verdict": "clear", "note": "", "issues": []}
        note = (r.get("note") or "; ".join(r.get("issues") or []))[:400]
        if r["verdict"] == "block":
            db.update("content", cid, {"status": "blocked", "compliance_verdict": "block", "compliance_note": note})
        elif r["verdict"] == "revise":
            db.update("content", cid, {"status": "in_review", "compliance_verdict": "revise", "compliance_note": note})
            to_revise.append(cid)
            live_ids.append(cid)
        else:
            db.update("content", cid, {"status": "pending_approval", "compliance_verdict": "clear", "compliance_note": note or None})
            db.insert("approvals", {"content_id": cid, "requested_by": "copywriter"})
            live_ids.append(cid)
    designer_dep = task["id"]
    if to_revise:
        rev_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "copywriter",
                                     "title": f"Rewrite {len(to_revise)} post(s) sent back by Sofia",
                                     "input": {"mode": "revise", "revise_content_ids": to_revise},
                                     "depends_on": [task["id"]], "status": "ready", "round": task["round"]})
        designer_dep = rev_id
    if live_ids:
        des_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "designer",
                                     "title": f"Design posters for {len(live_ids)} posts (round {task['round']})",
                                     "input": {"content_ids": live_ids}, "depends_on": [designer_dep],
                                     "status": "blocked", "round": task["round"]})
        pub = _publisher_task(brief["id"], task["round"])
        if pub:
            _add_dep(pub["id"], des_id)
    if out.get("summary"):
        post_message(brief["id"], "compliance", "board", "report", out["summary"], task["id"])


def hook_seo(task: dict, out: dict, brief: dict) -> None:
    db.update("briefs", brief["id"], {"seo_title": out.get("page_title") or None,
                                      "meta_description": out.get("meta_description") or None})


def hook_designer(task: dict, out: dict, brief: dict) -> None:
    content_ids = task["input"].get("content_ids", [])
    specs = {a["content_index"]: a for a in out["assets"]}
    redesign = bool(task["input"].get("redesign"))
    subject = out.get("subject_keywords") or []
    used = {a["photo_url"] for a in db.query("assets", "photo_url IS NOT NULL")
            if (db.get("content", a["content_id"]) or {}).get("brief_id") == brief["id"]}
    for i, cid in enumerate(content_ids):
        c = db.get("content", cid)
        if not c or c["status"] == "blocked":
            continue
        if redesign and not specs.get(i):
            specs[i] = {"content_index": i, "headline": c["headline"], "subline": c["cta"], "palette": {},
                        "layout": ("split", "frame", "photo")[(c.get("redesigns") or 0) % 3], "treatment": "dark",
                        "photo_query": f"{brief['product_name']} {c['headline']}", "alt_text": c["headline"]}
        spec = specs.get(i) or {"content_index": i, "headline": c["headline"], "subline": c["cta"],
                                "palette": {}, "layout": ("photo", "split", "frame")[i % 3], "treatment": "dark",
                                "photo_query": f"{brief['product_name']} {c['headline']}", "alt_text": c["headline"]}
        photo = tools.find_photo(spec.get("photo_query") or f"{brief['product_name']} {c['headline']}", exclude=used, subject=subject)
        if photo:
            used.add(photo.get("url") or photo.get("source_url"))
        svg = tools.render_poster_svg(spec, brief["product_name"], photo)
        aid = db.insert("assets", {"content_id": cid, "kind": "svg_poster", "spec": spec, "svg": svg,
                                   "alt_text": spec.get("alt_text") or c["headline"],
                                   "photo_path": photo["path"] if photo else None,
                                   "photo_credit": photo["credit"] if photo else None,
                                   "photo_url": (photo.get("url") or photo.get("source_url")) if photo else None})
        if redesign:
            db.update("content", cid, {"asset_id": aid, "status": "pending_approval",
                                       "redesigns": (c.get("redesigns") or 0) + 1})
            db.insert("approvals", {"content_id": cid, "requested_by": "designer", "round": c["round"]})
        else:
            db.update("content", cid, {"asset_id": aid})


def hook_publisher(task: dict, out: dict, brief: dict) -> None:
    approved = _content_for_round(brief["id"], task["round"], ("approved",))
    slots = {s["content_index"]: s["publish_slot"] for s in out.get("schedule", [])}
    for i, c in enumerate(approved):
        db.update("content", c["id"], {"status": "published", "published_at": db.now(),
                                       "published_day": int(brief.get("day") or 0),
                                       "publish_slot": slots.get(i) or f"Day {i + 1} 09:00"})
    db.update("briefs", brief["id"], {"status": "live", "campaign_headline": out["campaign_headline"],
                                      "campaign_intro": out["campaign_intro"]})
    post_message(brief["id"], "publisher", "board", "status",
                 f"Round {task['round']} is live: {len(approved)} posts published. Campaign page: /campaign/{brief['id']}",
                 task["id"])
    rnd = task["round"]
    if rnd == 1:
        db.insert("tasks", {"brief_id": brief["id"], "agent_key": "cfo", "title": "Closing memo for round 1",
                            "input": {"trigger": "campaign_end"}, "depends_on": [], "status": "ready", "round": 1})
        db.insert("tasks", {"brief_id": brief["id"], "agent_key": "motion", "title": "Storyboard the campaign video",
                            "input": {}, "depends_on": [], "status": "ready", "round": 1})
    pm_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "paid_media",
                                "title": "Allocate the ad budget" if rnd == 1 else f"Reallocate the ad budget after day {brief.get('day')}",
                                "input": {"round": rnd}, "depends_on": [], "status": "ready", "round": rnd})
    db.insert("tasks", {"brief_id": brief["id"], "agent_key": "ceo", "title": f"Board report, round {rnd}",
                        "input": {"mode": "board_report"}, "depends_on": [pm_id], "status": "blocked", "round": rnd})


def _record_cut(video_id: str, brief: dict, spec: dict, suffix: str = "") -> Path:
    MEDIA_DIR.mkdir(exist_ok=True)
    html_path = MEDIA_DIR / f"{video_id}{suffix}.html"
    html_path.write_text(tools.render_motion_html(spec, brief["product_name"], loop=False), encoding="utf-8")
    out = MEDIA_DIR / f"{video_id}{suffix}.webm"
    tools.record_motion_video(str(html_path), tools.motion_duration(spec), str(out))
    return out


def _generate_scene_clips(video_id: str, brief: dict, spec: dict) -> int:
    """Generate one clip per scene on the local GPUs, in parallel. Returns how many succeeded."""
    from concurrent.futures import ThreadPoolExecutor
    scenes = [sc for sc in spec["scenes"] if sc.get("video_prompt") or sc.get("photo_query")]
    gpus = tools.visible_gpus()
    if not scenes or not gpus:
        return 0

    def one(i_sc):
        i, sc = i_sc
        out = tools.GEN_DIR / f"{video_id}-{i}.mp4"
        res = tools.generate_clip(tools.scene_prompt(sc, brief), str(out), seconds=min(4.0, float(sc.get("seconds") or 3)),
                                  gpu=gpus[i % len(gpus)], seed=7 + i)
        return i, res

    done = 0
    with ThreadPoolExecutor(max_workers=len(gpus)) as ex:
        for i, res in ex.map(one, list(enumerate(scenes))):
            if res.get("ok"):
                scenes[i]["clip"] = res["path"]
                scenes[i]["clip_credit"] = "Generated by Jonas on our GPUs"
                scenes[i]["generated"] = True
                done += 1
    return done


def _render_video_async(video_id: str, brief: dict, spec: dict) -> None:
    def work():
        try:
            out = _record_cut(video_id, brief, spec)
            db.update("videos", video_id, {"status": "ready", "path": str(out)})
            post_message(brief["id"], "motion", "board", "status",
                         f"First cut of the campaign video is up ({tools.motion_duration(spec):.0f}s, real footage). "
                         + ("I am now generating our own shots for it on the GPUs; the new cut replaces this one when done."
                            if tools.video_generation_available() else "It is on the campaign page."))
        except Exception as e:  # keep the HTML animation as the deliverable
            log.warning("video render failed: %s", e)
            db.update("videos", video_id, {"status": "html_only", "error": str(e)[:300]})
            return
        if not tools.video_generation_available():
            return
        try:
            db.update("videos", video_id, {"status": "generating"})
            n = _generate_scene_clips(video_id, brief, spec)
            if n:
                out = _record_cut(video_id, brief, spec, "-gen")
                db.update("videos", video_id, {"status": "ready", "path": str(out), "spec": spec, "generated": 1})
                post_message(brief["id"], "motion", "board", "status",
                             f"Generated cut is in: {n} of {len(spec['scenes'])} scenes shot by our own model, "
                             "straight from the storyboard. It replaced the stock-footage cut on the campaign page.")
            else:
                db.update("videos", video_id, {"status": "ready", "error": "generation produced no clips"})
        except Exception as e:
            log.warning("generated cut failed: %s", e)
            db.update("videos", video_id, {"status": "ready", "error": str(e)[:300]})
    threading.Thread(target=work, name="video-render", daemon=True).start()


def hook_motion(task: dict, out: dict, brief: dict) -> None:
    scenes, used = [], set()
    subject = out.get("subject_keywords") or []
    for sc in out["scenes"]:
        sc = dict(sc)
        q = sc.get("photo_query")
        clip = tools.find_video(q, exclude=used, subject=subject) if q else None
        photo = tools.find_photo(q, exclude=used, subject=subject) if (q and not clip) else None
        for m in (clip, photo):
            if m:
                used.add(m.get("source_url") or m.get("url"))
                used.add(m.get("url") or "")
        sc["clip"] = clip["path"] if clip else None
        sc["clip_credit"] = clip["credit"] if clip else None
        sc["photo"] = photo["path"] if photo else None
        sc["photo_credit"] = photo["credit"] if photo else None
        scenes.append(sc)
    spec = {"title": out["title"], "scenes": scenes}
    vid = db.insert("videos", {"brief_id": brief["id"], "task_id": task["id"], "title": out["title"], "spec": spec,
                               "caption": out.get("caption", ""), "status": "rendering",
                               "duration_s": tools.motion_duration(spec)})
    if tools.env_flag("MOTION_RENDER", "1"):
        _render_video_async(vid, brief, spec)
    else:
        db.update("videos", vid, {"status": "html_only"})


def hook_paid_media(task: dict, out: dict, brief: dict) -> None:
    db.insert("ad_plans", {"brief_id": brief["id"], "task_id": task["id"], "round": task["round"],
                           "allocation": out["allocation"], "expected_cpa_eur": out["expected_cpa_eur"],
                           "rationale": out["rationale"]})
    total = sum(float(a.get("daily_eur") or 0) for a in out["allocation"])
    split = ", ".join(f"{a['channel']} {a['share_pct']}%" for a in out["allocation"])
    post_message(brief["id"], "paid_media", "board", "report",
                 f"Ad plan round {task['round']}: EUR {total:.0f}/day ({split}). Expected cost per signup EUR "
                 f"{out['expected_cpa_eur']:.2f}. {out['rationale']}", task["id"])


def hook_community(task: dict, out: dict, brief: dict) -> None:
    day = task["input"].get("day")
    by_label = {c["label"]: c for c in db.query("comments", "brief_id = ? AND day = ?", [brief["id"], day])}
    n = 0
    for r in out["replies"]:
        c = by_label.get(r["comment_id"])
        if c:
            db.update("comments", c["id"], {"reply": r["reply"], "replied_at": db.now()})
            n += 1
    s = out["sentiment"]
    post_message(brief["id"], "community", "board", "report",
                 f"Day {day}: replied to {n} comments. Mood {s['positive']} positive, {s['neutral']} neutral, "
                 f"{s['negative']} negative. Themes: {', '.join(out['themes'][:3])}."
                 + (f" Escalating: {out['escalations'][0]}" if out.get("escalations") else ""), task["id"])


def hook_analyst(task: dict, out: dict, brief: dict) -> None:
    ranked = published_ranking(brief["id"])
    by_label = {p["label"]: p for p in ranked}
    by_ctr = sorted(ranked, key=lambda p: -(p["ctr_per_day"] or 0))
    report = out["report"]
    winner = by_label.get(out.get("winner_content_id"))
    loser = by_label.get(out.get("loser_content_id"))
    if by_ctr:
        top, bottom = by_ctr[0], by_ctr[-1]
        if (winner and winner["id"] != top["id"]) or (loser and len(by_ctr) > 1 and loser["id"] != bottom["id"]) \
                or not winner or (not loser and len(by_ctr) > 1):
            winner, loser = top, (bottom if len(by_ctr) > 1 else None)
            report = report.rstrip() + f" (winner {top['label']}, loser {bottom['label'] if len(by_ctr) > 1 else '-'} "\
                                       "by ctr_per_day; corrected by the runtime)"
            out["winner_content_id"], out["loser_content_id"] = top["label"], (bottom["label"] if len(by_ctr) > 1 else "")
    out["report"] = report
    post_message(brief["id"], "analyst", "board", "report", report, task["id"])

    # ---- directives change the plan, not just a text field
    recs = out.get("recommendations") or []
    num_posts, channel_focus, focus, hold = 2, None, None, False
    chans = active_channels(brief)
    seen_research = False
    for r in recs:
        d = r.get("directive") or "new_posts"
        if d == "drop_channel" and r.get("channel") in chans and len(chans) > 1:
            chans = [c for c in chans if c != r["channel"]]
            post_message(brief["id"], "analyst", "board", "alert",
                         f"Mei dropped {r['channel']}: {r.get('why') or r.get('action')}", task["id"])
        elif d == "boost_channel" and r.get("channel"):
            channel_focus = r["channel"]
            num_posts += 1
        elif d == "re_research" and not seen_research:
            seen_research = True
            focus = r.get("why") or r.get("action")
        elif d == "hold":
            hold = True
        elif d == "new_posts" and r.get("num_posts"):
            num_posts = int(r["num_posts"])
    if channel_focus and channel_focus not in chans:
        channel_focus = None
    num_posts = max(1, min(4, num_posts))
    db.update("briefs", brief["id"], {"active_channels": chans})

    next_round = _current_round(brief["id"]) + 1
    analysis = {"report": report, "findings": out["findings"], "recommendations": recs,
                "winner": {k: winner[k] for k in ("channel", "headline", "ctr_per_day", "signups")} if winner else None,
                "loser": {k: loser[k] for k in ("channel", "headline", "ctr_per_day", "signups")} if loser else None,
                "day": brief.get("day")}
    db.update("tasks", task["id"], {"output": {**out, "winner_id": winner["id"] if winner else None,
                                               "loser_id": loser["id"] if loser else None}})
    if hold:
        post_message(brief["id"], "analyst", "board", "status",
                     "Mei: hold. " + next((r.get("why") or r.get("action")) for r in recs if r.get("directive") == "hold"),
                     task["id"])
        return

    deps = []
    if focus:
        r_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "researcher",
                                   "title": f"Re-research: {focus[:70]}", "input": {"focus": focus},
                                   "depends_on": [], "status": "ready", "round": next_round})
        deps.append(r_id)
    for key in ("researcher", "strategist"):
        last = db.query("tasks", "brief_id = ? AND agent_key = ? AND status = 'done' AND json_extract(input,'$.mode') IS NULL",
                        [brief["id"], key], order="created_at DESC", limit=1)
        if last:
            deps.append(last[0]["id"])
    s_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "strategist",
                               "title": f"Revise the strategy after day {brief.get('day')} results",
                               "input": {"revision": True, "analysis": analysis}, "depends_on": deps,
                               "status": "blocked", "round": next_round})
    c_input = {"num_posts": num_posts,
               "recommendations": [r for r in recs if r.get("target_agent") == "copywriter"] or recs}
    if channel_focus:
        c_input["channel_focus"] = channel_focus
    seo_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "seo",
                                 "title": f"Fix the wording of what did not work (round {next_round})",
                                 "input": {"revision": True}, "depends_on": [s_id], "status": "blocked", "round": next_round})
    c_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "copywriter",
                               "title": f"Write round {next_round} posts on the analyst's recommendations",
                               "input": c_input, "depends_on": [s_id, seo_id], "status": "blocked", "round": next_round})
    db.insert("tasks", {"brief_id": brief["id"], "agent_key": "publisher",
                        "title": f"Ship round {next_round}", "input": {}, "depends_on": [c_id],
                        "status": "blocked", "round": next_round})
    db.update("briefs", brief["id"], {"status": "iterating"})


def board_decide(content: dict, decision: str, note: str, target: str | None) -> dict:
    """Approve or veto one post. Vetoes are routed to Lena, Kofi or both; a design-only veto
    triggers a redesign instead of killing the post."""
    target = target or (classify_note(note) if note else "copywriter")
    redesign = decision == "vetoed" and target == "designer" and bool(content.get("asset_id"))
    db.update("content", content["id"], {"status": "redesign" if redesign else decision})
    for a in db.query("approvals", "content_id = ? AND decision IS NULL", [content["id"]]):
        db.update("approvals", a["id"], {"decision": decision, "decided_by": "board", "note": note,
                                         "target": target if decision == "vetoed" else None, "round": content["round"]})
    verb = "approved" if decision == "approved" else "vetoed"
    body = f"Board {verb} the {content['channel']} post \"{content['headline']}\"."
    if note:
        body += f" Note: {note}"
    to = "publisher" if verb == "approved" else {"designer": "designer", "both": "all"}.get(target, "copywriter")
    post_message(content["brief_id"], "board", to, "status" if verb == "approved" else "alert", body)
    if redesign:
        db.insert("tasks", {"brief_id": content["brief_id"], "agent_key": "designer",
                            "title": f"Redesign the poster for \"{content['headline'][:40]}\"",
                            "input": {"content_ids": [content["id"]], "redesign": True, "note": note},
                            "depends_on": [], "status": "ready", "round": content["round"]})
    return {"status": "redesign" if redesign else decision, "target": target}


def hook_cfo(task: dict, out: dict, brief: dict) -> None:
    kind = "alert" if out["action"] == "pause" or out["burn_assessment"] == "critical" else "report"
    post_message(brief["id"], "cfo", "board", kind, out["memo"], task["id"])
    if out["action"] == "pause":
        pause_company("CFO paused the company: " + out["memo"][:200], by="cfo")


def hook_mode_answer(task: dict, out: dict, brief: dict) -> None:
    post_message(brief["id"], task["agent_key"], task["input"].get("from_agent", "all"), "answer", out["answer"], task["id"])


def hook_mode_standup(task: dict, out: dict, brief: dict) -> None:
    for u in out["updates"]:
        if db.get("agents", u["agent"], id_col="key"):
            post_message(brief["id"], u["agent"], "all", "standup", u["line"], task["id"])


def hook_mode_board_report(task: dict, out: dict, brief: dict) -> None:
    db.insert("reports", {"brief_id": brief["id"], "task_id": task["id"], "round": task["round"],
                          "day": brief.get("day") or 0, "headline": out["headline"], "body": out})


HOOKS = {"ceo": hook_ceo, "seo": hook_seo, "copywriter": hook_copywriter, "compliance": hook_compliance, "designer": hook_designer,
         "publisher": hook_publisher, "motion": hook_motion, "paid_media": hook_paid_media,
         "community": hook_community, "analyst": hook_analyst, "cfo": hook_cfo}
MODE_HOOKS = {"answer": hook_mode_answer, "standup": hook_mode_standup, "board_report": hook_mode_board_report,
              "revise": hook_copywriter}


# ------------------------------------------------------------------ run_agent

def _variant(task: dict) -> str | None:
    mode = task["input"].get("mode")
    if mode:
        return mode
    if task["agent_key"] == "cfo" and task["input"].get("trigger") == "budget_80":
        return "budget"
    return f"r{task['round']}" if task["round"] > 1 else None


def _handle_question(task: dict, out: dict, brief: dict) -> None:
    q = out.get("question_for_colleague")
    if not q or not isinstance(q, dict):
        return
    target = q.get("agent")
    if not target or target == task["agent_key"] or not db.get("agents", target, id_col="key"):
        return
    post_message(brief["id"], task["agent_key"], target, "question", q["question"], task["id"])
    db.insert("tasks", {"brief_id": brief["id"], "agent_key": target,
                        "title": f"Answer {agent_name(task['agent_key'])}'s question",
                        "input": {"mode": "answer", "question": q["question"], "from_agent": task["agent_key"]},
                        "depends_on": [], "status": "ready", "round": task["round"]})


def run_agent(task: dict) -> bool:
    agent_key = task["agent_key"]
    agent = db.get("agents", agent_key, id_col="key")
    brief = db.get("briefs", task["brief_id"])
    if not agent or not brief:
        db.update("tasks", task["id"], {"status": "failed", "error": "missing agent or brief"})
        return False
    mode = task["input"].get("mode")
    db.update("tasks", task["id"], {"status": "running", "attempt": task["attempt"] + 1,
                                    "started_at": db.now(), "error": None})
    db.update("agents", agent_key, {"status": "working", "current_task_id": task["id"]}, id_col="key")
    log.info("%s starts: %s", agent_key, task["title"])
    started = time.time()
    try:
        user = build_user_message(task, brief)
        output, tin, tout = call_and_validate(agent, agent["system_prompt"], user, mode, _variant(task))
        # demo pacing: every task takes at least company.pace_seconds so people can follow the handoffs
        pace = float(company().get("pace_seconds") or 0)
        remaining = pace - (time.time() - started)
        if remaining > 0:
            time.sleep(remaining)
    except Exception as e:
        log.error("%s failed: %s", agent_key, e)
        db.update("tasks", task["id"], {"status": "failed", "error": str(e)[:500], "finished_at": db.now()})
        db.update("agents", agent_key, {"status": "blocked", "current_task_id": None}, id_col="key")
        post_message(brief["id"], agent_key, "board", "alert",
                     f"I could not finish '{task['title']}': {str(e)[:160]}. Board, retry me from the dashboard.",
                     task["id"])
        return False

    record_spend(agent_key, task["id"], tin, tout)
    db.update("tasks", task["id"], {"status": "done", "output": output, "finished_at": db.now()})
    db.update("agents", agent_key, {"status": "idle", "current_task_id": None}, id_col="key")
    kind = MODES[mode]["kind"] if mode else KIND.get(agent_key, "handoff")
    to = task["input"].get("from_agent", "all") if mode == "answer" else TO_AGENT.get(agent_key, "copywriter")
    if mode != "answer":  # the answer itself is posted by the hook; no separate chatter
        post_message(brief["id"], agent_key, to, kind, output["message_to_team"], task["id"])
    hook = MODE_HOOKS.get(mode) if mode else HOOKS.get(agent_key)
    try:
        if hook:
            hook(db.get("tasks", task["id"]), output, db.get("briefs", brief["id"]))
        _handle_question(task, output, brief)
    except Exception as e:
        log.error("hook %s failed: %s\n%s", agent_key, e, traceback.format_exc())
        post_message(brief["id"], agent_key, "board", "alert", f"Post-processing failed: {str(e)[:200]}", task["id"])
    budget_guard(brief["id"])
    return True


# ------------------------------------------------------------------ orchestrator

def publisher_gate_open(task: dict) -> bool:
    rows = _content_for_round(task["brief_id"], task["round"])
    if not rows:
        return False
    return all(c["status"] in DECIDED for c in rows) and any(c["status"] == "approved" for c in rows)


def _deps_done(task: dict) -> bool:
    for dep_id in task["depends_on"] or []:
        dep = db.get("tasks", dep_id)
        if not dep or dep["status"] != "done":
            return False
    return True


def next_runnable() -> dict | None:
    for t in db.query("tasks", "status = 'blocked'"):
        if _deps_done(t):
            db.update("tasks", t["id"], {"status": "ready"})
    for t in db.query("tasks", "status IN ('ready','waiting_human')", order="created_at ASC"):
        if t["agent_key"] == "publisher":
            if publisher_gate_open(t):
                if t["status"] != "ready":
                    db.update("tasks", t["id"], {"status": "ready"})
                return t
            if t["status"] != "waiting_human":
                db.update("tasks", t["id"], {"status": "waiting_human"})
            continue
        return t
    return None


def tick(max_tasks: int = 60) -> int:
    """Run every runnable task, oldest first. Returns number of tasks run."""
    ran = 0
    while ran < max_tasks:
        if company()["paused"]:
            break
        task = next_runnable()
        if task is None:
            break
        run_agent(task)
        ran += 1
    return ran


_stop = threading.Event()


def orchestrator_loop() -> None:
    interval = float(os.environ.get("TICK_SECONDS", "1.5"))
    log.info("orchestrator started (tick %.1fs, mock_llm=%s)", interval, tools.env_flag("MOCK_LLM"))
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            log.error("tick crashed:\n%s", traceback.format_exc())
        _stop.wait(interval)


def start_orchestrator() -> threading.Thread:
    _stop.clear()
    t = threading.Thread(target=orchestrator_loop, name="orchestrator", daemon=True)
    t.start()
    return t


def stop_orchestrator() -> None:
    _stop.set()


# ------------------------------------------------------------------ board-facing operations

def create_brief(b: dict) -> str:
    """Insert a brief and the CEO's first task. `b` has the BriefIn fields."""
    known = ("instagram", "linkedin", "x")
    channels = [c for c in (str(x).strip().lower() for x in (b.get("channels") or [])) if c in known]
    channels = list(dict.fromkeys(channels)) or list(known)
    row = {k: b.get(k, "") for k in ("product_name", "one_liner", "description", "audience", "goals", "tone")}
    row.update({"budget_eur": float(b.get("budget_eur") or 0), "channels": channels, "active_channels": channels,
                "status": "new", "day": 0})
    bid = db.insert("briefs", row)
    db.insert("tasks", {"brief_id": bid, "agent_key": "ceo", "title": f"Plan the campaign for {row['product_name']}",
                        "input": {}, "depends_on": [], "status": "ready", "round": 1})
    post_message(bid, "board", "ceo", "handoff", f"New brief from the board: {row['product_name']}. Iris, it is yours.")
    return bid


def simulate_day(brief_id: str) -> dict:
    """One simulated day: metrics, comments, standup, community, analyst. Raises ValueError if not possible."""
    brief = db.get("briefs", brief_id)
    if not brief:
        raise ValueError("no such brief")
    published = db.query("content", "brief_id = ? AND status = 'published'", [brief_id])
    if not published:
        raise ValueError("nothing published yet")
    if db.query("tasks", "brief_id = ? AND agent_key = 'analyst' AND status IN ('ready','running','blocked')", [brief_id]):
        raise ValueError("analyst is already working on the last day")
    day = (brief.get("day") or 0) + 1
    if tools.env_flag("MOCK_LLM") and day > tools.MOCK_MAX_ROUNDS:
        raise ValueError(f"mock mode supports {tools.MOCK_MAX_ROUNDS} simulated days; set MOCK_LLM=0 for more")
    plans = db.query("ad_plans", "brief_id = ?", [brief_id], order="created_at DESC", limit=1)
    rows = tools.simulate_metrics(brief, day, published)
    tools.apply_paid_media(rows, published, plans[0]["allocation"] if plans else None, f"{brief_id}:{day}")
    for row in rows:
        db.insert("metrics", row)
    for cm in tools.simulate_comments(brief, day, published):
        db.insert("comments", cm)
    db.update("briefs", brief_id, {"day": day})
    rnd = _current_round(brief_id)
    post_message(brief_id, "board", "all", "status",
                 f"Day {day} is over. Iris, run the standup. Pim, the comments are in. Mei, tell us what happened.")
    db.insert("tasks", {"brief_id": brief_id, "agent_key": "ceo", "title": f"Day {day} standup",
                        "input": {"mode": "standup", "day": day}, "depends_on": [], "status": "ready", "round": rnd})
    cm_id = db.insert("tasks", {"brief_id": brief_id, "agent_key": "community", "title": f"Reply to day {day} comments",
                                "input": {"day": day}, "depends_on": [], "status": "ready", "round": rnd})
    db.insert("tasks", {"brief_id": brief_id, "agent_key": "analyst", "title": f"Analyse day {day} results",
                        "input": {"day": day}, "depends_on": [cm_id], "status": "blocked", "round": rnd})
    return {"ok": True, "day": day, "content": len(published)}


# ------------------------------------------------------------------ chat with the CEO

def _chat_state(brief: dict | None) -> dict:
    c = company()
    st = {"company": {"paused": bool(c["paused"]), "spent_eur": round(c["spent_eur"], 2), "budget_eur": c["budget_eur"]},
          "agents_working": [{"name": agent_name(a["key"]), "on": (db.get("tasks", a["current_task_id"]) or {}).get("title")}
                             for a in db.query("agents", "status = 'working'")],
          "brief": None}
    if not brief:
        return st
    tasks = db.query("tasks", "brief_id = ?", [brief["id"]], order="created_at ASC")
    content = db.query("content", "brief_id = ?", [brief["id"]])
    pending = [x for x in content if x["status"] == "pending_approval"]
    st["brief"] = {"product_name": brief["product_name"], "campaign_name": brief.get("campaign_name"),
                   "status": brief["status"], "day": brief.get("day") or 0, "round": _current_round(brief["id"]),
                   "active_channels": active_channels(brief), "objective": brief.get("objective")}
    st["pending_approvals"] = [{"channel": x["channel"], "headline": x["headline"]} for x in pending]
    st["content"] = {"published": len([x for x in content if x["status"] == "published"]),
                     "vetoed": len([x for x in content if x["status"] == "vetoed"]),
                     "blocked_by_compliance": len([x for x in content if x["status"] == "blocked"])}
    st["tasks"] = [{"agent": agent_name(t["agent_key"]), "title": t["title"], "status": t["status"]}
                   for t in tasks if t["status"] not in ("done",)][-8:]
    perf = [p for p in post_performance(brief["id"]) if p["status"] == "published"]
    if perf:
        st["performance"] = [{k: p[k] for k in ("round", "channel", "headline", "days_live", "ctr_per_day", "signups", "ad_spend_eur")}
                             for p in perf]
    an = _latest_output(brief["id"], "analyst")
    if an:
        st["latest_analyst_report"] = an.get("report")
    rep = db.query("reports", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
    if rep:
        st["latest_board_report"] = {"headline": rep[0]["headline"], "next": rep[0]["body"].get("next")}
    plan = db.query("ad_plans", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
    if plan:
        st["ad_plan"] = plan[0]["allocation"]
    vid = db.query("videos", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=1)
    if vid:
        st["video"] = {"title": vid[0]["title"], "status": vid[0]["status"]}
    st["recent_bus"] = [f"{agent_name(m['from_agent']) if m['from_agent'] != 'board' else 'Board'}: {m['body'][:160]}"
                        for m in reversed(db.query("messages", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=8))]
    return st


AGENT_ALIASES = {"nora": "researcher", "yara": "seo", "bram": "strategist", "lena": "copywriter", "sofia": "compliance", "kofi": "designer",
                 "tariq": "publisher", "jonas": "motion", "jules": "paid_media", "pim": "community", "mei": "analyst",
                 "otto": "cfo", "iris": "ceo"}


def _mock_chat(message: str, brief: dict | None, st: dict) -> dict:
    """Deterministic Iris for mock mode: understands briefs, days, pause/resume, status, team notes."""
    m = message.lower().strip()
    if not brief:
        if any(w in m for w in ("nachtfiets", "demo", "bike light")):
            demo = json.loads((ROOT / "demo_brief.json").read_text(encoding="utf-8"))
            return {"reply": "Nachtfiets, the bike-light subscription. Good brief, I know Amsterdam. I am starting the "
                             "company on it now: Nora maps the market first, Bram plans, Lena writes, and you will see the "
                             "posts in your inbox with posters before anything goes live.",
                    "actions": [{"type": "start_brief", "brief": demo}]}
        if len(m.split()) >= 8:
            words = [w.strip(".,!?") for w in message.split()]
            name = next((w for w in words[:12] if w[:1].isupper() and w.lower() not in ("i", "we", "our", "it", "a")), "Your product")
            brief_out = {"product_name": name, "one_liner": message.strip()[:160], "description": message.strip(),
                         "audience": "people who have the problem this solves", "goals": "300 signups in 2 weeks",
                         "tone": "clear, confident, a little cheeky", "budget_eur": 500, "channels": ["instagram", "linkedin", "x"]}
            return {"reply": f"Understood. I am starting the company on {name}. You did not give me an audience, a goal or a "
                             "budget, so I assumed a consumer audience, 300 signups in two weeks and EUR 500; tell me if that is "
                             "wrong and I will correct the brief. Nora starts on the market now.",
                    "actions": [{"type": "start_brief", "brief": brief_out}]}
        return {"reply": "Hi, I am Iris. I run the team here, and you are the boss. Tell me about the thing you sell: what it "
                         "is, who buys it, and what you would like to happen in the next two weeks. A few sentences are enough; "
                         "I will work out the rest and get the team going.", "actions": []}
    b = st["brief"]
    if any(w in m for w in ("pause", "stop everything", "kill")):
        return {"reply": "Pausing everything now. The team finishes what it is doing this second and then stops. Say resume when you want them back.",
                "actions": [{"type": "pause"}]}
    if any(w in m for w in ("resume", "continue", "unpause", "start again")):
        return {"reply": "Resuming. The team picks up where it stopped.", "actions": [{"type": "resume"}]}
    if any(w in m for w in ("next day", "simulate", "a day", "run a day", "advance", "results")):
        if st["content"]["published"] == 0:
            return {"reply": "Nothing is live yet, so there is no day to run. " + (f"{len(st['pending_approvals'])} posts are waiting for your approval in the inbox." if st.get("pending_approvals") else "Lena is still writing."), "actions": []}
        return {"reply": f"Moving on to day {b['day'] + 1}. Pim will answer what people wrote under the posts, Mei will look at "
                         "how each post did, and Bram will change the plan if something is not working. Give it a minute.",
                "actions": [{"type": "simulate_day"}]}
    for alias, key in AGENT_ALIASES.items():
        if key != "ceo" and re.search(rf"\b(tell|ask|let)\b.*\b{alias}\b", m):
            note = re.split(rf"\b{alias}\b", message, flags=re.I)[-1].strip(" :,to") or message
            return {"reply": f"Passed to {alias.capitalize()}: \"{note}\". It goes on the team channel and into the next task.",
                    "actions": [{"type": "message_team", "agent": key, "note": note}]}
    pending = st.get("pending_approvals") or []
    lines = [f"{b['product_name']}, campaign {b.get('campaign_name') or 'in planning'}, {b['status']}, day {b['day']}, round {b['round']}."]
    if pending:
        lines.append(f"{len(pending)} post(s) are waiting for your yes or no in the inbox: " + "; ".join(p["headline"] for p in pending[:3]) + ".")
    if st.get("performance"):
        top = max(st["performance"], key=lambda p: p["ctr_per_day"] or 0)
        pct = round(100 * (top['ctr_per_day'] or 0), 1)
        lines.append(f"The post doing best is \"{top['headline']}\" on {top['channel']}: about {pct} out of every 100 people who saw it clicked, and {top['signups']} signed up.")
    if st.get("latest_analyst_report"):
        lines.append("Mei's last read: " + st["latest_analyst_report"][:200])
    if st.get("agents_working"):
        w = st["agents_working"][0]
        lines.append(f"Right now {w['name']} is on: {w['on']}.")
    lines.append(f"Spend EUR {st['company']['spent_eur']:.2f} of EUR {st['company']['budget_eur']:.2f}.")
    return {"reply": " ".join(lines), "actions": []}


def chat(message: str) -> dict:
    """Board member talks to Iris. Stores both turns, executes actions, returns the CEO turn."""
    message = (message or "").strip()[:2000]
    if not message:
        raise ValueError("empty message")
    brief_id = current_brief_id()
    brief = db.get("briefs", brief_id) if brief_id else None
    db.insert("chat", {"brief_id": brief_id, "role": "board", "body": message, "actions": []})
    st = _chat_state(brief)
    history = db.query("chat", order="created_at DESC", limit=12)
    if tools.env_flag("MOCK_LLM"):
        out = _mock_chat(message, brief, st)
        tin = tout = 0
    else:
        user = ("## Company state right now\n" + _j(st) + "\n\n## Recent chat (oldest first)\n"
                + "\n".join(f"{'Board' if h['role'] == 'board' else 'Iris'}: {h['body']}" for h in reversed(history[1:]))
                + f"\n\n## The board member just said\n{message}")
        system = CHAT_PROMPT + "\n\nRespond with a single JSON object matching this schema and nothing else:\n" + json.dumps(CHAT_SCHEMA)
        text, tin, tout = tools.call_llm("ceo", system, user, CHAT_SCHEMA, "chat")
        out = models.ChatOut.model_validate(tools.parse_json(text)).model_dump()
    done = []
    for a in out.get("actions") or []:
        t = a.get("type")
        try:
            if t == "start_brief" and a.get("brief"):
                bid = create_brief(a["brief"])
                done.append({"type": t, "label": f"started the company on {a['brief']['product_name']}", "brief_id": bid})
                brief_id = bid
            elif t == "simulate_day" and brief_id:
                r = simulate_day(brief_id)
                done.append({"type": t, "label": f"day {r['day']} is running"})
            elif t == "pause":
                if not company()["paused"]:
                    pause_company("Board paused the company through Iris.")
                done.append({"type": t, "label": "company paused"})
            elif t == "resume":
                if company()["paused"]:
                    resume_company()
                done.append({"type": t, "label": "company resumed"})
            elif t == "message_team" and a.get("agent") and a.get("note"):
                key = AGENT_ALIASES.get(a["agent"].lower(), a["agent"])
                if db.get("agents", key, id_col="key"):
                    post_message(brief_id, "ceo", key, "handoff", f"From the board, via me: {a['note']}")
                    done.append({"type": t, "label": f"told {agent_name(key)}"})
        except ValueError as e:
            out["reply"] = out["reply"].rstrip() + f" (I could not do that: {e}.)"
    if tin or tout:
        record_spend("ceo", "chat", tin, tout)
    cid = db.insert("chat", {"brief_id": brief_id, "role": "ceo", "body": out["reply"], "actions": done})
    return {"id": cid, "reply": out["reply"], "actions": done}
