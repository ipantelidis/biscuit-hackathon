"""T2 + T4: analyst directives change the task graph; vetoes are routed and expire."""
import db
import runtime


def _live_campaign(client):
    bid = client.post("/api/briefs/demo").json()["brief_id"]
    runtime.tick()
    s = client.get("/api/state").json()
    for p in s["pending_approvals"]:
        client.post(f"/api/approvals/{p['id']}", json={"decision": "approved"})
    runtime.tick()
    client.post(f"/api/briefs/{bid}/simulate_day")
    return bid


def _fake_analyst(bid, recs, winner="C1", loser="C2"):
    task = db.query("tasks", "brief_id = ? AND agent_key = 'analyst'", [bid])[-1]
    out = {"report": "fake", "winner_content_id": winner, "loser_content_id": loser, "findings": [],
           "recommendations": recs, "message_to_team": "x"}
    runtime.hook_analyst(task, out, db.get("briefs", bid))
    return task


def test_drop_channel_removes_it_and_discards_items(client):
    bid = _live_campaign(client)
    _fake_analyst(bid, [{"action": "a", "why": "dead", "target_agent": "strategist", "directive": "drop_channel",
                         "channel": "linkedin", "num_posts": None}])
    b = db.get("briefs", bid)
    assert b["active_channels"] == ["instagram", "x"]
    cw = db.query("tasks", "brief_id = ? AND agent_key = 'copywriter' AND round = 2", [bid])[0]
    prompt = runtime.build_user_message(cw, b)
    active_block = prompt.split("## Active channels (write only for these)")[1].split("Dropped by the analyst")[0]
    assert '"linkedin"' not in active_block and "Dropped by the analyst" in prompt
    fake = {"items": [{"channel": "linkedin", "headline": "LI", "body": "b", "cta": "", "hashtags": [], "rationale": "", "risk_flags": []},
                      {"channel": "x", "headline": "X post", "body": "b", "cta": "", "hashtags": [], "rationale": "", "risk_flags": []}],
            "message_to_team": "m"}
    runtime.hook_copywriter(cw, fake, b)
    rows = db.query("content", "brief_id = ? AND round = 2", [bid])
    assert [r["channel"] for r in rows] == ["x"]
    assert any(m["kind"] == "alert" and "dropped channel" in m["body"] for m in db.query("messages", "brief_id = ?", [bid]))


def test_re_research_creates_researcher_task_the_strategist_depends_on(client):
    bid = _live_campaign(client)
    _fake_analyst(bid, [{"action": "a", "why": "student bike parking hotspots", "target_agent": "strategist",
                         "directive": "re_research", "channel": None, "num_posts": None}])
    r = db.query("tasks", "brief_id = ? AND agent_key = 'researcher' AND round = 2", [bid])
    assert len(r) == 1 and r[0]["input"]["focus"] == "student bike parking hotspots"
    st = db.query("tasks", "brief_id = ? AND agent_key = 'strategist' AND round = 2", [bid])[0]
    assert r[0]["id"] in st["depends_on"]
    runtime.tick()
    assert db.get("tasks", r[0]["id"])["status"] == "done"


def test_hold_creates_no_chain(client):
    bid = _live_campaign(client)
    _fake_analyst(bid, [{"action": "wait", "why": "not yet", "target_agent": "strategist", "directive": "hold",
                         "channel": None, "num_posts": None}])
    assert not db.query("tasks", "brief_id = ? AND agent_key = 'copywriter' AND round = 2", [bid])
    assert db.get("briefs", bid)["status"] == "live"
    assert any("Mei: hold" in m["body"] for m in db.query("messages", "brief_id = ?", [bid]))


def test_winner_override_by_ctr_per_day(client):
    bid = _live_campaign(client)
    ranked = sorted(runtime.published_ranking(bid), key=lambda p: -(p["ctr_per_day"] or 0))
    top, bottom = ranked[0], ranked[-1]
    wrong_w = next(p["label"] for p in ranked if p["id"] != top["id"])
    task = _fake_analyst(bid, [{"action": "a", "why": "w", "target_agent": "copywriter", "directive": "new_posts",
                                "channel": None, "num_posts": 3}], winner=wrong_w, loser=top["label"])
    t = db.get("tasks", task["id"])
    assert t["output"]["winner_id"] == top["id"] and t["output"]["loser_id"] == bottom["id"]
    assert "corrected by the runtime" in t["output"]["report"]
    cw = db.query("tasks", "brief_id = ? AND agent_key = 'copywriter' AND round = 2", [bid])[0]
    assert cw["input"]["num_posts"] == 3


def test_veto_routing_and_expiry(client):
    bid = client.post("/api/briefs/demo").json()["brief_id"]
    runtime.tick()
    s = client.get("/api/state").json()
    pend = s["pending_approvals"]
    r = client.post(f"/api/approvals/{pend[0]['id']}", json={"decision": "vetoed", "note": "headline overflows the poster"}).json()
    assert r["status"] == "redesign" and r["target"] == "designer"
    client.post(f"/api/approvals/{pend[1]['id']}", json={"decision": "vetoed", "note": "too salesy"})
    client.post(f"/api/approvals/{pend[2]['id']}", json={"decision": "approved"})
    apps = {a["content_id"]: a for a in db.query("approvals", "decision = 'vetoed'")}
    assert apps[pend[0]["id"]]["target"] == "designer" and apps[pend[1]["id"]]["target"] == "copywriter"
    # redesign: Kofi re-renders, the post comes back to the board with a new poster, publisher waited
    old_asset = pend[0]["asset_id"]
    runtime.tick()
    s = client.get("/api/state").json()
    again = [p for p in s["pending_approvals"] if p["id"] == pend[0]["id"]]
    assert again and again[0]["asset_id"] != old_asset and again[0]["redesigns"] == 1
    assert db.query("tasks", "brief_id = ? AND agent_key = 'publisher' AND round = 1", [bid])[0]["status"] == "waiting_human"
    client.post(f"/api/approvals/{pend[0]['id']}", json={"decision": "approved"})
    runtime.tick()
    client.post(f"/api/briefs/{bid}/simulate_day")
    runtime.tick()
    b = db.get("briefs", bid)
    cw = db.query("tasks", "brief_id = ? AND agent_key = 'copywriter' AND round = 2", [bid])[0]
    des = db.query("tasks", "brief_id = ? AND agent_key = 'designer' AND round = 2", [bid])[0]
    assert "too salesy" in runtime.build_user_message(cw, b)
    assert "headline overflows" not in runtime.build_user_message(cw, b)
    assert "headline overflows" in runtime.build_user_message(des, b)
    # a round-1 copy veto is gone by round 4
    cw4 = {**cw, "round": 4}
    assert "too salesy" not in runtime.build_user_message(cw4, b)
