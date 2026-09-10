"""Ghost Boosters: FastAPI app, routes, startup (init db, seed agents, start orchestrator)."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import db  # noqa: E402
import runtime  # noqa: E402
import tools  # noqa: E402
from agents import seed_rows  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("ghost")

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def init_db() -> None:
    db.connect()
    db.seed_company("Ghost Boosters", float(os.environ.get("BUDGET_EUR", "5.0")))
    db.seed_agents(seed_rows())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if tools.env_flag("ORCHESTRATOR_AUTOSTART", "1"):
        runtime.start_orchestrator()
    yield
    runtime.stop_orchestrator()


app = FastAPI(title="Ghost Boosters", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ------------------------------------------------------------------ pages

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "static" / "dashboard.html")


@app.get("/campaign/{brief_id}", response_class=HTMLResponse)
def campaign_page(brief_id: str, request: Request):
    brief = db.get("briefs", brief_id)
    if not brief:
        raise HTTPException(404, "no such campaign")
    posts = db.query("content", "brief_id = ? AND status = 'published'", [brief_id],
                     order="round ASC, published_at ASC")
    videos = db.query("videos", "brief_id = ?", [brief_id], order="created_at DESC", limit=1)
    video = videos[0] if videos else None
    if video and video.get("status") == "ready" and not (video.get("path") and Path(video["path"]).exists()):
        video = {**video, "status": "html_only"}  # recorded file gone: play the live animation instead
    comments = db.query("comments", "brief_id = ? AND reply IS NOT NULL", [brief_id], order="created_at DESC", limit=6)
    post_titles = {p["id"]: p["headline"] for p in posts}
    for c in comments:
        c["on_post"] = post_titles.get(c["content_id"], "")
    for p in posts:
        asset = db.get("assets", p["asset_id"]) if p.get("asset_id") else None
        p["photo_credit"] = asset.get("photo_credit") if asset else None
        p["photo_url"] = (asset.get("spec") or {}).get("_source_url") if asset and (asset.get("spec") or {}).get("_source_url") else (asset.get("photo_url") if asset else None)
        ms = db.query("metrics", "content_id = ?", [p["id"]])
        if ms:
            imp = sum(m["impressions"] for m in ms)
            clk = sum(m["clicks"] for m in ms)
            p["metrics"] = {"impressions": imp, "clicks": clk, "likes": sum(m["likes"] for m in ms),
                            "signups": sum(m["signups"] for m in ms),
                            "ctr": round(100 * clk / imp, 1) if imp else 0.0, "days": len(ms)}
        else:
            p["metrics"] = None
    footage = []
    if video:
        for sc in (video.get("spec") or {}).get("scenes", []):
            cr = sc.get("clip_credit") or sc.get("photo_credit")
            if cr and cr not in footage:
                footage.append(cr)
    return templates.TemplateResponse(request, "campaign.html",
                                      {"brief": brief, "posts": posts, "day": brief.get("day") or 0,
                                       "video": video, "comments": comments, "footage": footage})


@app.get("/motion/{brief_id}", response_class=HTMLResponse, include_in_schema=False)
def motion_page(brief_id: str, loop: int = 1):
    videos = db.query("videos", "brief_id = ?", [brief_id], order="created_at DESC", limit=1)
    if not videos:
        raise HTTPException(404, "no storyboard yet")
    brief = db.get("briefs", brief_id)
    return HTMLResponse(tools.render_motion_html(videos[0]["spec"], brief["product_name"] if brief else "", loop=bool(loop),
                                                 clip_url=lambda path: f"/api/media/clips/{Path(path).name}"))


@app.get("/api/media/clips/{name}", include_in_schema=False)
def clip_file(name: str):
    if "/" in name or ".." in name or not name.endswith(".mp4"):
        raise HTTPException(404, "no such clip")
    path = tools.CLIP_DIR / name
    if not path.exists():
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/videos/{video_id}.webm", include_in_schema=False)
def video_file(video_id: str):
    v = db.get("videos", video_id)
    if not v or v.get("status") != "ready" or not v.get("path") or not Path(v["path"]).exists():
        raise HTTPException(404, "video not ready")
    return FileResponse(v["path"], media_type="video/webm")


@app.get("/api/content/{content_id}/asset.svg", include_in_schema=False)
def asset_svg(content_id: str):
    c = db.get("content", content_id)
    asset = db.get("assets", c["asset_id"]) if c and c.get("asset_id") else None
    if not asset:
        raise HTTPException(404, "no asset")
    return Response(asset["svg"], media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------ state

@app.get("/api/state")
def state():
    c = runtime.company()
    agents = db.query("agents", order="rowid ASC")
    brief_id = runtime.current_brief_id()
    brief = db.get("briefs", brief_id) if brief_id else None
    tasks = db.query("tasks", "brief_id = ?", [brief_id], order="round ASC, created_at ASC") if brief_id else []
    messages = db.query("messages", order="created_at DESC", limit=50)
    content = db.query("content", "brief_id = ?", [brief_id], order="round ASC, created_at ASC") if brief_id else []
    pending = [x for x in content if x["status"] == "pending_approval"]
    metrics = db.query("metrics", "brief_id = ?", [brief_id]) if brief_id else []
    by_content: dict[str, dict] = {}
    for m in metrics:
        agg = by_content.setdefault(m["content_id"], {"impressions": 0, "clicks": 0, "likes": 0, "signups": 0, "days": 0})
        for k in ("impressions", "clicks", "likes", "signups"):
            agg[k] += m[k]
        agg["days"] += 1
    for cid, agg in by_content.items():
        agg["ctr"] = round(100 * agg["clicks"] / agg["impressions"], 1) if agg["impressions"] else 0.0
    for x in content:
        x["has_asset"] = bool(x.get("asset_id"))
        x["metrics"] = by_content.get(x["id"])
    for a in agents:
        a.pop("system_prompt", None)
        a.pop("output_schema", None)
        t = db.get("tasks", a["current_task_id"]) if a.get("current_task_id") else None
        a["current_task_title"] = t["title"] if t else None
    for t in tasks:
        t.pop("output", None)
        t["mode"] = (t.get("input") or {}).get("mode")
        t.pop("input", None)
    videos = db.query("videos", "brief_id = ?", [brief_id], order="created_at DESC", limit=1) if brief_id else []
    if videos and videos[0].get("status") == "ready" and not (videos[0].get("path") and Path(videos[0]["path"]).exists()):
        videos[0]["status"] = "html_only"
    if videos:
        videos[0]["has_footage"] = any(sc.get("clip") or sc.get("photo") for sc in (videos[0].get("spec") or {}).get("scenes", []))
    comments = db.query("comments", "brief_id = ?", [brief_id], order="created_at DESC", limit=40) if brief_id else []
    titles = {x["id"]: x["headline"] for x in content}
    for cm in comments:
        cm["on_post"] = titles.get(cm["content_id"], "")
    ad_plans = db.query("ad_plans", "brief_id = ?", [brief_id], order="created_at DESC", limit=1) if brief_id else []
    reports = db.query("reports", "brief_id = ?", [brief_id], order="created_at DESC", limit=1) if brief_id else []
    ad_spend = round(sum(m["ad_spend_eur"] or 0 for m in metrics), 2)
    return {
        "company": c,
        "mock": {"llm": tools.env_flag("MOCK_LLM"), "search": tools.env_flag("MOCK_SEARCH")},
        "model": os.environ.get("LLM_MODEL", "claude-sonnet-5"),
        "agents": agents,
        "brief": brief,
        "tasks": tasks,
        "messages": list(reversed(messages)),
        "content": content,
        "pending_approvals": pending,
        "spend": {"spent_eur": c["spent_eur"], "budget_eur": c["budget_eur"],
                  "by_agent": {a["key"]: a["cost_eur"] for a in agents}},
        "metrics": by_content,
        "video": videos[0] if videos else None,
        "comments": list(reversed(comments)),
        "ad_plan": ad_plans[0] if ad_plans else None,
        "ad_spend_eur": ad_spend,
        "report": reports[0] if reports else None,
        "chat": list(reversed(db.query("chat", order="created_at DESC", limit=40))),
    }


# ------------------------------------------------------------------ actions

class BriefIn(BaseModel):
    product_name: str = Field(..., min_length=1)
    one_liner: str = ""
    description: str = ""
    audience: str = ""
    goals: str = ""
    budget_eur: float = 0
    tone: str = ""
    channels: list[str] = Field(default_factory=lambda: ["instagram", "linkedin", "x"])


def create_brief(b: BriefIn) -> str:
    return runtime.create_brief(b.model_dump())


@app.post("/api/briefs")
def post_brief(b: BriefIn):
    return {"brief_id": create_brief(b)}


@app.post("/api/briefs/demo")
def post_demo_brief(which: int = 1):
    path = ROOT / ("demo_brief.json" if which == 1 else "demo_brief_2.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"brief_id": create_brief(BriefIn(**data))}


class Decision(BaseModel):
    decision: str = Field(..., pattern="^(approved|vetoed)$")
    note: str = ""
    target: str | None = Field(None, pattern="^(copywriter|designer|both)$")


@app.post("/api/approvals/{content_id}")
def decide(content_id: str, d: Decision):
    c = db.get("content", content_id)
    if not c:
        raise HTTPException(404, "no such content")
    if c["status"] != "pending_approval":
        raise HTTPException(409, f"content is {c['status']}")
    return {"ok": True, **runtime.board_decide(c, d.decision, d.note, d.target)}


@app.post("/api/briefs/{brief_id}/simulate_day")
def simulate_day(brief_id: str):
    try:
        return runtime.simulate_day(brief_id)
    except ValueError as e:
        raise HTTPException(404 if "no such" in str(e) else 409, str(e))


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


@app.post("/api/chat")
def chat(c: ChatIn):
    try:
        return runtime.chat(c.message)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # the model call failed; keep the chat usable
        log.exception("chat failed")
        raise HTTPException(502, f"Iris could not answer: {str(e)[:160]}")


@app.get("/api/chat")
def chat_history(limit: int = 40):
    return {"chat": list(reversed(db.query("chat", order="created_at DESC", limit=limit)))}


@app.post("/api/briefs/{brief_id}/video")
def redo_video(brief_id: str):
    """Ask Jonas for a fresh storyboard (with real footage); the runtime renders and records it."""
    brief = db.get("briefs", brief_id)
    if not brief:
        raise HTTPException(404, "no such brief")
    if not db.query("content", "brief_id = ? AND status = 'published'", [brief_id]):
        raise HTTPException(409, "nothing published yet")
    if db.query("tasks", "brief_id = ? AND agent_key = 'motion' AND status IN ('ready','running')", [brief_id]):
        raise HTTPException(409, "Jonas is already on it")
    db.insert("tasks", {"brief_id": brief_id, "agent_key": "motion", "title": "Redo the campaign video",
                        "input": {"redo": True}, "depends_on": [], "status": "ready", "round": runtime._current_round(brief_id)})
    runtime.post_message(brief_id, "board", "motion", "status", "Jonas, the board wants a new cut of the campaign video.")
    return {"ok": True}


@app.post("/api/company/pause")
def pause():
    if not runtime.company()["paused"]:
        runtime.pause_company("Board paused the company. All agents stop after their current call.")
    return {"ok": True, "paused": True}


class Pace(BaseModel):
    seconds: float = Field(..., ge=0, le=60)


@app.post("/api/company/pace")
def set_pace(p: Pace):
    db.update("company", "company", {"pace_seconds": p.seconds})
    return {"ok": True, "pace_seconds": p.seconds}


@app.post("/api/company/resume")
def resume():
    if runtime.company()["paused"]:
        runtime.resume_company()
    return {"ok": True, "paused": False}


@app.post("/api/tasks/{task_id}/retry")
def retry(task_id: str):
    t = db.get("tasks", task_id)
    if not t:
        raise HTTPException(404, "no such task")
    if t["status"] != "failed":
        raise HTTPException(409, f"task is {t['status']}")
    db.update("tasks", task_id, {"status": "ready", "attempt": 0, "error": None})
    db.update("agents", t["agent_key"], {"status": "idle", "current_task_id": None}, id_col="key")
    runtime.post_message(t["brief_id"], "board", t["agent_key"], "status", f"Board asked for a retry of '{t['title']}'.")
    return {"ok": True}


@app.post("/api/reset")
def reset():
    db.reset_all()
    for f in runtime.MEDIA_DIR.glob("*"):
        if f.is_file():  # keep media/photos: licensed photos are reusable across runs
            try:
                f.unlink()
            except OSError:
                pass
    return {"ok": True}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    t = db.get("tasks", task_id)
    if not t:
        raise HTTPException(404, "no such task")
    return t
