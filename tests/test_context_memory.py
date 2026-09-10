"""T1: an agent in round N sees what the company already knows."""
import db
import runtime


def _run_round_one(client):
    bid = client.post("/api/briefs/demo").json()["brief_id"]
    runtime.tick()
    s = client.get("/api/state").json()
    for p in s["pending_approvals"]:
        client.post(f"/api/approvals/{p['id']}", json={"decision": "approved"})
    runtime.tick()
    client.post(f"/api/briefs/{bid}/simulate_day")
    runtime.tick()
    return bid


def test_revision_strategist_sees_research_and_previous_strategy(client):
    bid = _run_round_one(client)
    t = db.query("tasks", "brief_id = ? AND agent_key = 'strategist' AND round = 2", [bid])[0]
    prompt = runtime.build_user_message(t, db.get("briefs", bid))
    assert "## Output of researcher" in prompt
    assert "## Output of strategist" in prompt
    assert "## Strategy changelog" in prompt and "## Content performance so far" in prompt
    assert t["output"]["changes_from_previous_round"]


def test_round_two_copywriter_sees_round_one_posts_with_numbers(client):
    bid = _run_round_one(client)
    t = db.query("tasks", "brief_id = ? AND agent_key = 'copywriter' AND round = 2 AND json_extract(input,'$.mode') IS NULL", [bid])[0]
    prompt = runtime.build_user_message(t, db.get("briefs", bid))
    first = db.query("content", "brief_id = ? AND round = 1 AND status = 'published'", [bid])[0]
    assert first["body"][:60] in prompt
    assert "ctr_per_day" in prompt and "Do not reuse a headline" in prompt
    assert "## Active channels" in prompt


def test_analyst_prompt_has_per_day_rows_and_ranking(client):
    bid = _run_round_one(client)
    t = db.query("tasks", "brief_id = ? AND agent_key = 'analyst'", [bid])[0]
    prompt = runtime.build_user_message(t, db.get("briefs", bid))
    assert "## Ranking by ctr_per_day" in prompt and '"per_day"' in prompt and '"days_live"' in prompt
