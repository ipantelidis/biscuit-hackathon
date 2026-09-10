"""The board's chat with Iris: she acts, not just talks (mock mode)."""
import runtime


def test_chat_starts_company_runs_day_and_routes_notes(client):
    r = client.post("/api/chat", json={"message": "hi"}).json()
    assert "Iris" in r["reply"] and r["actions"] == []
    r = client.post("/api/chat", json={"message": "We make Nachtfiets, a bike-light subscription for Amsterdam cyclists; go."}).json()
    assert r["actions"][0]["type"] == "start_brief"
    s = client.get("/api/state").json()
    assert s["brief"]["product_name"] == "Nachtfiets" and len(s["chat"]) == 4
    runtime.tick()
    r = client.post("/api/chat", json={"message": "How is it going?"}).json()
    assert "waiting for you" in r["reply"] and r["actions"] == []
    r = client.post("/api/chat", json={"message": "Run the next day please"}).json()
    assert r["actions"] == [] and "Nothing is live" in r["reply"]
    r = client.post("/api/chat", json={"message": "Tell Lena to be less salesy"}).json()
    assert r["actions"][0]["type"] == "message_team" and r["actions"][0]["label"] == "told Lena"
    assert any(m["from_agent"] == "ceo" and m["to_agent"] == "copywriter" and "less salesy" in m["body"]
               for m in client.get("/api/state").json()["messages"])
    for p in client.get("/api/state").json()["pending_approvals"]:
        client.post(f"/api/approvals/{p['id']}", json={"decision": "approved"})
    runtime.tick()
    r = client.post("/api/chat", json={"message": "Run a day"}).json()
    assert r["actions"][0]["type"] == "simulate_day" and client.get("/api/state").json()["brief"]["day"] == 1
    r = client.post("/api/chat", json={"message": "pause everything"}).json()
    assert r["actions"][0]["type"] == "pause" and client.get("/api/state").json()["company"]["paused"] == 1
    r = client.post("/api/chat", json={"message": "resume"}).json()
    assert r["actions"][0]["type"] == "resume" and client.get("/api/state").json()["company"]["paused"] == 0
    assert client.get("/api/chat").json()["chat"][-1]["role"] == "ceo"


def test_chat_with_own_product(client):
    r = client.post("/api/chat", json={"message": "Our product is Factuurtje, invoicing for Dutch freelancers that chases late payers politely until they pay."}).json()
    assert r["actions"][0]["type"] == "start_brief"
    assert client.get("/api/state").json()["brief"]["product_name"] == "Factuurtje"


def test_create_brief_normalises_channels(client):
    bid = runtime.create_brief({"product_name": "X", "channels": ["Social", "email", "X", "linkedin"]})
    import db
    assert db.get("briefs", bid)["channels"] == ["x", "linkedin"]
    bid = runtime.create_brief({"product_name": "Y", "channels": ["paid"]})
    assert db.get("briefs", bid)["channels"] == ["instagram", "linkedin", "x"]
