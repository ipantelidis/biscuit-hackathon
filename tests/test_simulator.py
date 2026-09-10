"""T3: the simulator rewards different things on different days."""
import tools

BRIEF = {"id": "b-sim", "channels": ["instagram", "linkedin", "x"]}


def post(i, headline, channel, published_day=0, risk=None):
    return {"id": f"c{i}", "headline": headline, "channel": channel, "published_day": published_day,
            "published_at": f"2026-09-10T00:0{i}:00", "risk_flags": risk or []}


def test_fatigue_halves_a_repeated_opening():
    rows = tools.simulate_metrics(BRIEF, 0, [post(1, "Lost 3 lights this year?", "x"),
                                             post(2, "Lost 3 lights this year again", "x")])
    assert rows[1]["factors"]["fatigue"] == 0.5
    assert rows[1]["ctr"] < 0.6 * rows[0]["ctr"]


def test_decay_after_three_days():
    one = [post(1, "Never ride dark", "instagram")]
    d0 = tools.simulate_metrics(BRIEF, 0, one)[0]
    d3 = tools.simulate_metrics(BRIEF, 3, one)[0]
    assert d3["impressions"] < 0.5 * d0["impressions"]
    assert d3["factors"]["decay"] == 0.45


def test_saturation_of_number_led_posts():
    three = [post(1, "3 lights, 0 worries", "x"), post(2, "9 euro beats 60", "instagram"), post(3, "24 hours to new lights", "linkedin")]
    rows = tools.simulate_metrics(BRIEF, 1, three)
    assert all(r["factors"]["saturation"] == 0.75 for r in rows)


def test_risk_flags_and_channel_fit_and_determinism():
    posts = [post(1, "Never ride dark", "instagram", risk=["price promise"]), post(2, "Never ride dark", "linkedin")]
    a = tools.simulate_metrics(BRIEF, 1, posts)
    b = tools.simulate_metrics(BRIEF, 1, posts)
    assert a == b
    assert a[0]["factors"]["risk"] == 0.9 and a[0]["factors"]["channel_fit"] == 1.15
    assert len(a) == 2 and all(set(r) >= {"impressions", "clicks", "signups", "ctr", "factors"} for r in a)
