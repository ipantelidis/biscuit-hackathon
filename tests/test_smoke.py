"""Whole company in mock mode: brief -> round 1 live -> simulate day -> round 2 live."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "MOCK_LLM": "1", "MOCK_SEARCH": "1", "MOCK_LLM_DELAY": "0", "MOTION_RENDER": "0",
    "ORCHESTRATOR_AUTOSTART": "0", "BUDGET_EUR": "5.0", "PACE_SECONDS": "0",
    "GHOST_DB_PATH": str(ROOT / "tests" / "_smoke.db"),
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import runtime  # noqa: E402


@pytest.fixture()
def client():
    for suffix in ("", "-wal", "-shm"):
        p = Path(os.environ["GHOST_DB_PATH"] + suffix)
        if p.exists():
            p.unlink()
    db.close()
    import main
    with TestClient(main.app) as c:
        yield c
    db.close()


def statuses(tasks):
    out = {}
    for t in tasks:
        out[(t["agent_key"], t["round"], t.get("mode"))] = t["status"]
    return out


def kinds(messages):
    return {m["kind"] for m in messages}


def test_full_loop(client):
    s = client.get("/api/state").json()
    assert len(s["agents"]) == 12 and s["brief"] is None

    bid = client.post("/api/briefs/demo").json()["brief_id"]
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    # round 1 up to the board gate
    assert st[("ceo", 1, None)] == "done"
    assert st[("localizer", 1, None)] == "done"                      # hired specialist worked
    assert any(a["key"] == "localizer" and a["hired"] for a in s["agents"])
    assert st[("researcher", 1, "answer")] == "done"                 # Nora answered Bram's question
    assert {"question", "answer"} <= kinds(s["messages"])
    assert st[("compliance", 1, None)] == "done"
    assert st[("copywriter", 1, "revise")] == "done"                 # Sofia sent one post back
    assert st[("designer", 1, None)] == "done"
    assert st[("publisher", 1, None)] == "waiting_human"
    pending = s["pending_approvals"]
    assert len(pending) == 3 and all(p["has_asset"] for p in pending)
    revised = [c for c in s["content"] if c["compliance_verdict"] == "revised"]
    assert len(revised) == 1 and revised[0]["channel"] == "linkedin"
    assert client.get(f"/api/content/{pending[0]['id']}/asset.svg").headers["content-type"].startswith("image/svg")
    assert s["company"]["spent_eur"] > 0

    client.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "approved"})
    client.post(f"/api/approvals/{pending[1]['id']}", json={"decision": "approved"})
    client.post(f"/api/approvals/{pending[2]['id']}", json={"decision": "vetoed", "note": "too salesy"})
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("publisher", 1, None)] == "done" and st[("cfo", 1, None)] == "done"
    assert st[("motion", 1, None)] == "done" and st[("paid_media", 1, None)] == "done"
    assert st[("ceo", 1, "board_report")] == "done"
    assert s["brief"]["status"] == "live"
    assert s["video"] and s["video"]["status"] == "html_only" and s["video"]["title"]
    assert s["ad_plan"] and len(s["ad_plan"]["allocation"]) == 3
    assert s["report"] and s["report"]["headline"]
    assert len([c for c in s["content"] if c["status"] == "published"]) == 2
    page = client.get(f"/campaign/{bid}")
    assert page.status_code == 200 and "were employed in the making of this campaign" in page.text
    assert client.get(f"/motion/{bid}").status_code == 200

    # a day passes: standup, comments, community, analyst, round 2 chain, ad reallocation
    r = client.post(f"/api/briefs/{bid}/simulate_day").json()
    assert r["day"] == 1
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("ceo", 1, "standup")] == "done" and "standup" in kinds(s["messages"])
    assert st[("community", 1, None)] == "done"
    assert s["comments"] and all(c["reply"] for c in s["comments"])
    assert st[("analyst", 1, None)] == "done"
    assert st[("strategist", 2, None)] == st[("copywriter", 2, None)] == st[("compliance", 2, None)] == st[("designer", 2, None)] == "done"
    assert st[("publisher", 2, None)] == "waiting_human"
    assert s["brief"]["status"] == "iterating"
    assert s["ad_spend_eur"] > 0
    pending = s["pending_approvals"]
    assert len(pending) == 2 and all(p["round"] == 2 for p in pending)
    for p in pending:
        client.post(f"/api/approvals/{p['id']}", json={"decision": "approved"})
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("publisher", 2, None)] == "done" and st[("paid_media", 2, None)] == "done"
    assert st[("ceo", 2, "board_report")] == "done"
    assert s["ad_plan"]["round"] == 2
    assert len([c for c in s["content"] if c["status"] == "published"]) == 4
    assert "revised after day 1" in client.get(f"/campaign/{bid}").text
    assert not [t for t in s["tasks"] if t["status"] == "failed"]


def test_kill_switch_and_reset(client):
    client.post("/api/briefs/demo")
    client.post("/api/company/pause")
    assert runtime.tick() == 0
    s = client.get("/api/state").json()
    assert s["company"]["paused"] == 1 and all(a["status"] == "paused" for a in s["agents"])
    client.post("/api/company/resume")
    assert runtime.tick() >= 1
    client.post("/api/reset")
    s = client.get("/api/state").json()
    assert s["brief"] is None and s["company"]["spent_eur"] == 0 and len(s["agents"]) == 12


def test_budget_guard(client):
    db.update("company", "company", {"budget_eur": 0.02})
    client.post("/api/briefs/demo")
    runtime.tick()
    s = client.get("/api/state").json()
    assert s["company"]["paused"] == 1
    assert any(m["kind"] == "alert" and "Budget exhausted" in m["body"] for m in s["messages"])
