"""The nervous system: run_agent, orchestrator tick, post-processing hooks, budget guard."""
from __future__ import annotations

import json
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
from agents import AGENTS, MODES, SPECIALIST_SCHEMA, specialist_prompt

log = logging.getLogger("ghost.runtime")
ROOT = Path(__file__).parent
MEDIA_DIR = ROOT / "media"

TO_AGENT = {"ceo": "all", "researcher": "strategist", "strategist": "copywriter", "copywriter": "compliance",
            "compliance": "board", "designer": "board", "publisher": "all", "motion": "board",
            "paid_media": "board", "community": "analyst", "analyst": "strategist", "cfo": "board"}
KIND = {"ceo": "status", "researcher": "handoff", "strategist": "handoff", "copywriter": "handoff",
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
    vetoes = db.query("approvals", "decision = 'vetoed' AND note IS NOT NULL AND note != ''")
    notes = []
    for a in vetoes:
        c = db.get("content", a["content_id"])
        if c and c["brief_id"] == brief["id"]:
            notes.append({"channel": c["channel"], "headline": c["headline"], "board_note": a["note"]})
    if notes:
        parts.append("## Board veto notes on earlier posts (do not repeat these approaches)\n" + _j(notes))
    specialists = db.query("tasks", "brief_id = ? AND status = 'done' AND json_extract(input,'$.specialist') = 1",
                           [brief["id"]])
    for sp in specialists:
        parts.append(f"## Notes from {agent_name(sp['agent_key'])} (hired specialist)\n" + _j(sp["output"]))
    if task["round"] > 1:
        prev = db.query("content", "brief_id = ? AND round < ?", [brief["id"], task["round"]])
        if prev:
            parts.append("## Posts already published in earlier rounds (write different ones)\n"
                         + _j([{"round": c["round"], "channel": c["channel"], "headline": c["headline"],
                                "status": c["status"]} for c in prev]))
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
            items.append(_content_view(c, i))
    return f"## Content items to design (use content_index)\n{_j(items)}\n\nBrief tone: {brief.get('tone')}"


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
    parts = [f"## Metrics through day {brief.get('day')} per published post\n{_j(_metrics_rows(brief['id']))}"]
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


CONTEXT_BUILDERS = {"researcher": ctx_researcher, "copywriter": ctx_copywriter, "compliance": ctx_compliance,
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
    # hiring: create the specialist agent and a task that feeds the copywriter
    by_agent = {t["agent"]: tid for tid, t in created}
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
    for item in out["items"]:
        cid = db.insert("content", {"brief_id": brief["id"], "task_id": task["id"], "channel": item["channel"],
                                    "headline": item["headline"], "body": item["body"], "cta": item["cta"],
                                    "hashtags": item["hashtags"], "rationale": item["rationale"],
                                    "risk_flags": item["risk_flags"], "status": "in_review",
                                    "round": task["round"]})
        content_ids.append(cid)
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


def hook_designer(task: dict, out: dict, brief: dict) -> None:
    content_ids = task["input"].get("content_ids", [])
    specs = {a["content_index"]: a for a in out["assets"]}
    for i, cid in enumerate(content_ids):
        c = db.get("content", cid)
        if not c or c["status"] == "blocked":
            continue
        spec = specs.get(i) or {"content_index": i, "headline": c["headline"], "subline": c["cta"],
                                "palette": {}, "layout": ("stacked", "split", "badge")[i % 3], "glyph": "✦",
                                "alt_text": c["headline"]}
        svg = tools.render_poster_svg(spec, brief["product_name"])
        aid = db.insert("assets", {"content_id": cid, "kind": "svg_poster", "spec": spec, "svg": svg,
                                   "alt_text": spec.get("alt_text") or c["headline"]})
        db.update("content", cid, {"asset_id": aid})


def hook_publisher(task: dict, out: dict, brief: dict) -> None:
    approved = _content_for_round(brief["id"], task["round"], ("approved",))
    slots = {s["content_index"]: s["publish_slot"] for s in out.get("schedule", [])}
    for i, c in enumerate(approved):
        db.update("content", c["id"], {"status": "published", "published_at": db.now(),
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


def _render_video_async(video_id: str, brief: dict, spec: dict) -> None:
    def work():
        try:
            MEDIA_DIR.mkdir(exist_ok=True)
            html_path = MEDIA_DIR / f"{video_id}.html"
            html_path.write_text(tools.render_motion_html(spec, brief["product_name"], loop=False), encoding="utf-8")
            out = MEDIA_DIR / f"{video_id}.webm"
            tools.record_motion_video(str(html_path), tools.motion_duration(spec), str(out))
            db.update("videos", video_id, {"status": "ready", "path": str(out)})
            post_message(brief["id"], "motion", "board", "status",
                         f"The campaign video is rendered ({tools.motion_duration(spec):.0f}s). It is on the campaign page.")
        except Exception as e:  # keep the HTML animation as the deliverable
            log.warning("video render failed: %s", e)
            db.update("videos", video_id, {"status": "html_only", "error": str(e)[:300]})
    threading.Thread(target=work, name="video-render", daemon=True).start()


def hook_motion(task: dict, out: dict, brief: dict) -> None:
    spec = {"title": out["title"], "scenes": out["scenes"]}
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
    pub = db.query("content", "brief_id = ? AND status = 'published'", [brief["id"]])
    labels = {f"C{i + 1}": c for i, c in enumerate(pub)}
    winner = labels.get(out.get("winner_content_id"))
    loser = labels.get(out.get("loser_content_id"))
    post_message(brief["id"], "analyst", "board", "report", out["report"], task["id"])
    next_round = _current_round(brief["id"]) + 1
    analysis = {"report": out["report"], "findings": out["findings"], "recommendations": out["recommendations"],
                "winner": _content_view(winner) if winner else None,
                "loser": _content_view(loser) if loser else None, "day": brief.get("day")}
    s_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "strategist",
                               "title": f"Revise the strategy after day {brief.get('day')} results",
                               "input": {"revision": True, "analysis": analysis}, "depends_on": [],
                               "status": "ready", "round": next_round})
    c_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "copywriter",
                               "title": f"Write round {next_round} posts on the analyst's recommendations",
                               "input": {"num_posts": 2, "recommendations":
                                         [r for r in out["recommendations"] if r["target_agent"] == "copywriter"]
                                         or out["recommendations"]},
                               "depends_on": [s_id], "status": "blocked", "round": next_round})
    db.insert("tasks", {"brief_id": brief["id"], "agent_key": "publisher",
                        "title": f"Ship round {next_round}", "input": {}, "depends_on": [c_id],
                        "status": "blocked", "round": next_round})
    db.update("briefs", brief["id"], {"status": "iterating"})
    db.update("tasks", task["id"], {"output": {**out, "winner_id": winner["id"] if winner else None,
                                               "loser_id": loser["id"] if loser else None}})


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


HOOKS = {"ceo": hook_ceo, "copywriter": hook_copywriter, "compliance": hook_compliance, "designer": hook_designer,
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
