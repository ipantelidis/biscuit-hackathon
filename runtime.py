"""The nervous system: run_agent, orchestrator tick, post-processing hooks, budget guard."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback

from pydantic import ValidationError

import db
import models
import tools
from agents import AGENTS

log = logging.getLogger("ghost.runtime")

TO_AGENT = {"ceo": "all", "researcher": "strategist", "strategist": "copywriter",
            "copywriter": "board", "designer": "board", "publisher": "all",
            "analyst": "strategist", "cfo": "board"}
KIND = {"ceo": "status", "researcher": "handoff", "strategist": "handoff", "copywriter": "handoff",
        "designer": "handoff", "publisher": "status", "analyst": "report", "cfo": "report"}

BRIEF_FIELDS = ("product_name", "one_liner", "description", "audience", "goals",
                "budget_eur", "tone", "channels")


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
    vetoes = db.query("approvals", "decision = 'vetoed' AND note IS NOT NULL AND note != ''")
    notes = []
    for a in vetoes:
        c = db.get("content", a["content_id"])
        if c and c["brief_id"] == brief["id"]:
            notes.append({"channel": c["channel"], "headline": c["headline"], "board_note": a["note"]})
    if notes:
        parts.append("## Board veto notes on earlier posts (do not repeat these approaches)\n" + _j(notes))
    if task["round"] > 1:
        prev = db.query("content", "brief_id = ? AND round < ?", [brief["id"], task["round"]])
        if prev:
            parts.append("## Posts already published in earlier rounds (write different ones)\n"
                         + _j([{"round": c["round"], "channel": c["channel"], "headline": c["headline"],
                                "status": c["status"]} for c in prev]))
    return "\n\n".join(parts)


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


def _labelled_published(brief_id: str) -> list[tuple[str, dict]]:
    pub = db.query("content", "brief_id = ? AND status = 'published'", [brief_id], order="created_at ASC")
    return [(f"C{i + 1}", c) for i, c in enumerate(pub)]


def ctx_analyst(task: dict, brief: dict) -> str:
    rows = []
    for label, c in _labelled_published(brief["id"]):
        ms = db.query("metrics", "content_id = ?", [c["id"]])
        agg = {k: sum(m[k] for m in ms) for k in ("impressions", "clicks", "likes", "shares", "signups")}
        agg["ctr"] = round(agg["clicks"] / agg["impressions"], 4) if agg["impressions"] else 0.0
        rows.append({"content_id": label, "round": c["round"], "channel": c["channel"],
                     "headline": c["headline"], "body": c["body"][:200], "metrics": agg})
    strat = db.query("tasks", "brief_id = ? AND agent_key = 'strategist' AND status = 'done'",
                     [brief["id"]], order="created_at DESC", limit=1)
    parts = [f"## Metrics through day {brief.get('day')} per published post\n{_j(rows)}"]
    if strat:
        parts.append("## Current strategy\n" + _j(strat[0]["output"]))
    return "\n\n".join(parts)


def ctx_cfo(task: dict, brief: dict) -> str:
    c = company()
    agents = db.query("agents", order="cost_eur DESC")
    spend = [{"agent": a["key"], "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"],
              "cost_eur": round(a["cost_eur"], 4)} for a in agents]
    remaining = db.query("tasks", "brief_id = ? AND status NOT IN ('done','failed')", [brief["id"]])
    return (f"## Budget\nbudget_eur: {c['budget_eur']:.2f}\nspent_eur: {c['spent_eur']:.4f}\n"
            f"trigger: {task['input'].get('trigger', 'manual')}\n\n## Spend per agent\n{_j(spend)}\n\n"
            f"## Remaining planned tasks\n{_j([{'agent': t['agent_key'], 'title': t['title']} for t in remaining])}")


CONTEXT_BUILDERS = {"researcher": ctx_researcher, "copywriter": ctx_copywriter, "designer": ctx_designer,
                    "publisher": ctx_publisher, "analyst": ctx_analyst, "cfo": ctx_cfo}


def build_user_message(task: dict, brief: dict) -> str:
    parts = ["## Client brief\n" + _j(_brief_public(brief)),
             f"## Your task\nTitle: {task['title']}\nRound: {task['round']}\nInput: {_j(task['input'])}"]
    for dep_id in task["depends_on"] or []:
        dep = db.get("tasks", dep_id)
        if dep and dep.get("output"):
            parts.append(f"## Output of {dep['agent_key']} ({dep['title']})\n{_j(dep['output'])}")
    builder = CONTEXT_BUILDERS.get(task["agent_key"])
    if builder:
        extra = builder(task, brief)
        if extra:
            parts.append(extra)
    msgs = db.query("messages", "brief_id = ?", [brief["id"]], order="created_at DESC", limit=10)
    if msgs:
        parts.append("## Last messages on the team channel\n" + "\n".join(
            f"{m['from_agent']} -> {m['to_agent']} [{m['kind']}]: {m['body']}" for m in reversed(msgs)))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ LLM call with validation

def call_and_validate(agent_key: str, system: str, user: str, variant: str | None) -> tuple[dict, int, int]:
    schema = AGENTS[agent_key]["output_schema"]
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
            return models.validate(agent_key, data), tin_total, tout_total
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
    for esc in out.get("escalations") or []:
        post_message(brief["id"], "ceo", "board", "alert", f"Escalation: {esc}", task["id"])


def hook_copywriter(task: dict, out: dict, brief: dict) -> None:
    content_ids = []
    for item in out["items"]:
        cid = db.insert("content", {"brief_id": brief["id"], "task_id": task["id"], "channel": item["channel"],
                                    "headline": item["headline"], "body": item["body"], "cta": item["cta"],
                                    "hashtags": item["hashtags"], "rationale": item["rationale"],
                                    "risk_flags": item["risk_flags"], "status": "pending_approval",
                                    "round": task["round"]})
        db.insert("approvals", {"content_id": cid, "requested_by": "copywriter"})
        content_ids.append(cid)
    designer_id = db.insert("tasks", {"brief_id": brief["id"], "agent_key": "designer",
                                      "title": f"Design posters for {len(content_ids)} posts (round {task['round']})",
                                      "input": {"content_ids": content_ids}, "depends_on": [task["id"]],
                                      "status": "ready", "round": task["round"]})
    for pub in db.query("tasks", "brief_id = ? AND agent_key = 'publisher' AND round = ? AND status != 'done'",
                        [brief["id"], task["round"]]):
        db.update("tasks", pub["id"], {"depends_on": list(pub["depends_on"] or []) + [designer_id]})


def hook_designer(task: dict, out: dict, brief: dict) -> None:
    content_ids = task["input"].get("content_ids", [])
    specs = {a["content_index"]: a for a in out["assets"]}
    for i, cid in enumerate(content_ids):
        c = db.get("content", cid)
        if not c:
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
    if task["round"] == 1:
        db.insert("tasks", {"brief_id": brief["id"], "agent_key": "cfo", "title": "Closing memo for round 1",
                            "input": {"trigger": "campaign_end"}, "depends_on": [], "status": "ready", "round": 1})


def hook_analyst(task: dict, out: dict, brief: dict) -> None:
    labels = dict(_labelled_published(brief["id"]))
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


HOOKS = {"ceo": hook_ceo, "copywriter": hook_copywriter, "designer": hook_designer,
         "publisher": hook_publisher, "analyst": hook_analyst, "cfo": hook_cfo}


# ------------------------------------------------------------------ run_agent

def _variant(task: dict) -> str | None:
    if task["agent_key"] == "cfo" and task["input"].get("trigger") == "budget_80":
        return "budget"
    return f"r{task['round']}" if task["round"] > 1 else None


def run_agent(task: dict) -> bool:
    agent_key = task["agent_key"]
    agent = db.get("agents", agent_key, id_col="key")
    brief = db.get("briefs", task["brief_id"])
    if not agent or not brief:
        db.update("tasks", task["id"], {"status": "failed", "error": "missing agent or brief"})
        return False
    db.update("tasks", task["id"], {"status": "running", "attempt": task["attempt"] + 1,
                                    "started_at": db.now(), "error": None})
    db.update("agents", agent_key, {"status": "working", "current_task_id": task["id"]}, id_col="key")
    log.info("%s starts: %s", agent_key, task["title"])
    try:
        user = build_user_message(task, brief)
        output, tin, tout = call_and_validate(agent_key, agent["system_prompt"], user, _variant(task))
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
    post_message(brief["id"], agent_key, TO_AGENT[agent_key], KIND[agent_key], output["message_to_team"], task["id"])
    hook = HOOKS.get(agent_key)
    if hook:
        try:
            hook(db.get("tasks", task["id"]), output, db.get("briefs", brief["id"]))
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
    return all(c["status"] in ("approved", "vetoed", "published") for c in rows) and \
        any(c["status"] == "approved" for c in rows)


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


def tick(max_tasks: int = 50) -> int:
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
