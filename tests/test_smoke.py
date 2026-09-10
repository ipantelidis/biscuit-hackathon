"""Whole company in mock mode: brief -> round 1 live -> simulate day -> round 2 live."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "MOCK_LLM": "1", "MOCK_SEARCH": "1", "MOCK_LLM_DELAY": "0",
    "ORCHESTRATOR_AUTOSTART": "0", "BUDGET_EUR": "5.0",
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
    return {(t["agent_key"], t["round"]): t["status"] for t in tasks}


def test_full_loop(client):
    s = client.get("/api/state").json()
    assert len(s["agents"]) == 8 and s["brief"] is None

    bid = client.post("/api/briefs/demo").json()["brief_id"]
    assert runtime.tick() >= 1
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("ceo", 1)] == "done"
    assert s["brief"]["status"] == "in_progress"

    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("researcher", 1)] == st[("strategist", 1)] == st[("copywriter", 1)] == st[("designer", 1)] == "done"
    assert st[("publisher", 1)] == "waiting_human"
    pending = s["pending_approvals"]
    assert len(pending) == 3 and all(p["has_asset"] for p in pending)
    assert client.get(f"/api/content/{pending[0]['id']}/asset.svg").headers["content-type"].startswith("image/svg")
    assert s["company"]["spent_eur"] > 0

    client.post(f"/api/approvals/{pending[0]['id']}", json={"decision": "approved"})
    client.post(f"/api/approvals/{pending[1]['id']}", json={"decision": "approved"})
    client.post(f"/api/approvals/{pending[2]['id']}", json={"decision": "vetoed", "note": "too salesy"})
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("publisher", 1)] == "done" and st[("cfo", 1)] == "done"
    assert s["brief"]["status"] == "live"
    published = [c for c in s["content"] if c["status"] == "published"]
    assert len(published) == 2
    page = client.get(f"/campaign/{bid}")
    assert page.status_code == 200 and "were employed in the making of this campaign" in page.text

    # round 2: the loop closes without human prompting
    r = client.post(f"/api/briefs/{bid}/simulate_day").json()
    assert r["day"] == 1
    runtime.tick()
    s = client.get("/api/state").json()
    st = statuses(s["tasks"])
    assert st[("analyst", 1)] == "done"
    assert st[("strategist", 2)] == st[("copywriter", 2)] == st[("designer", 2)] == "done"
    assert st[("publisher", 2)] == "waiting_human"
    assert s["brief"]["status"] == "iterating"
    assert any(m["kind"] == "report" and m["from_agent"] == "analyst" for m in s["messages"])
    pending = s["pending_approvals"]
    assert len(pending) == 2 and all(p["round"] == 2 for p in pending)
    for p in pending:
        client.post(f"/api/approvals/{p['id']}", json={"decision": "approved"})
    runtime.tick()
    s = client.get("/api/state").json()
    assert statuses(s["tasks"])[("publisher", 2)] == "done"
    assert len([c for c in s["content"] if c["status"] == "published"]) == 4
    assert "revised after day 1" in client.get(f"/campaign/{bid}").text
    assert s["metrics"] and all(v["days"] == 1 for v in s["metrics"].values())


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
    assert s["brief"] is None and s["company"]["spent_eur"] == 0 and len(s["agents"]) == 8


def test_budget_guard(client):
    db.update("company", "company", {"budget_eur": 0.02})
    client.post("/api/briefs/demo")
    runtime.tick()
    s = client.get("/api/state").json()
    assert s["company"]["paused"] == 1
    assert any(m["kind"] == "alert" and "Budget exhausted" in m["body"] for m in s["messages"])
