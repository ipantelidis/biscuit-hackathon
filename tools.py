"""The hands of the company: LLM call, web search, poster renderer, publishing, metrics.

Every external call has a deterministic mock so the whole demo runs without keys.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("ghost.tools")
MOCK_DIR = Path(__file__).parent / "mocks"
MOCK_MAX_ROUNDS = 3   # rounds (and simulated days) mock mode supports before it says so


def env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ LLM

class LLMError(RuntimeError):
    pass


def _mock_llm(agent_key: str, variant: str | None) -> tuple[str, int, int]:
    delay = float(os.environ.get("MOCK_LLM_DELAY", "1.5"))
    if delay > 0:
        time.sleep(delay)
    candidates = []
    if variant:
        m = re.fullmatch(r"r(\d+)", variant)
        if m and int(m.group(1)) >= MOCK_MAX_ROUNDS + 1 and not (MOCK_DIR / f"{agent_key}_{variant}.json").exists():
            raise LLMError(f"mock mode supports {MOCK_MAX_ROUNDS} rounds; set MOCK_LLM=0 for more")
        candidates.append(MOCK_DIR / f"{agent_key}_{variant}.json")
        if variant in ("answer", "standup", "board_report", "revise"):  # mode: generic file beats the agent's own
            candidates.append(MOCK_DIR / f"{variant}.json")
    candidates.append(MOCK_DIR / f"{agent_key}.json")
    candidates.append(MOCK_DIR / "specialist.json")  # hired agents with dynamic keys
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            # plausible token counts so the spend meter moves in mock mode
            return text, 900 + len(text) // 4, len(text) // 3
    raise LLMError(f"no mock for {agent_key} (looked for {[str(c) for c in candidates]})")


_client = None


def _anthropic_client():
    global _client
    if _client is None:
        import anthropic  # imported lazily so mock mode needs no key
        _client = anthropic.Anthropic()
    return _client


def call_llm(agent_key: str, system: str, user: str, schema: dict | None = None,
             variant: str | None = None) -> tuple[str, int, int]:
    """Return (text, tokens_in, tokens_out). Raises LLMError on failure."""
    if env_flag("MOCK_LLM"):
        return _mock_llm(agent_key, variant)

    import anthropic

    model = os.environ.get("LLM_MODEL", "claude-sonnet-5")
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "2500"))
    effort = os.environ.get("LLM_EFFORT", "low")
    client = _anthropic_client()

    kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    output_config: dict = {"effort": effort}
    if schema is not None and env_flag("LLM_STRUCTURED", "1"):
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs["output_config"] = output_config

    try:
        resp = client.messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        # Structured-output schema rejected (or effort unsupported): fall back to prompt-only JSON.
        log.warning("LLM bad request for %s, retrying without output_config: %s", agent_key, e.message)
        kwargs.pop("output_config", None)
        try:
            resp = client.messages.create(**kwargs)
        except anthropic.APIError as e2:
            raise LLMError(f"{type(e2).__name__}: {getattr(e2, 'message', e2)}") from e2
    except anthropic.RateLimitError as e:
        raise LLMError(f"rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"connection error: {e}") from e

    if resp.stop_reason == "refusal":
        detail = getattr(resp, "stop_details", None)
        raise LLMError(f"model refused ({getattr(detail, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        log.warning("LLM output for %s hit max_tokens", agent_key)
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def parse_json(text: str) -> dict:
    """Strip code fences / prose around the first JSON object and parse."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            return json.loads(t[start:end + 1])
        raise


# ------------------------------------------------------------------ web search

def web_search(query: str, k: int = 5) -> list[dict]:
    """Tavily search -> [{title,url,snippet}]. Canned results only when the model is mocked too
    (a real model would be misled by off-topic canned sources); missing key -> empty."""
    if env_flag("MOCK_SEARCH") and env_flag("MOCK_LLM"):
        path = MOCK_DIR / "search.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))[:k]
        return []
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = httpx.post("https://api.tavily.com/search",
                       json={"api_key": key, "query": query, "max_results": k,
                             "search_depth": "basic"}, timeout=15)
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("content") or "")[:300]} for x in r.json().get("results", [])]
    except Exception as e:  # search is best-effort
        log.warning("web_search failed for %r: %s", query, e)
        return []


# ------------------------------------------------------------------ poster renderer

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT = "Helvetica Neue, Helvetica, Arial, Inter, system-ui, sans-serif"


def _hex(v: str, fallback: str) -> str:
    v = (v or "").strip()
    if _HEX.match(v):
        return v.lower()
    if re.match(r"^#[0-9a-fA-F]{3}$", v):
        return ("#" + "".join(c * 2 for c in v[1:])).lower()
    return fallback


def _wrap(text: str, max_chars: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, max_chars - 1)] + "…"
    return lines


CHAR_W = 0.58  # Helvetica bold average advance as a fraction of font size


def _fit(text: str, col_width: int, max_lines: int, sizes: tuple[int, ...]) -> tuple[list[str], int]:
    """Largest size in `sizes` whose wrapped lines all fit in col_width; falls back to the smallest."""
    text = " ".join(str(text or "").split())
    if not text:
        return [], sizes[-1]
    for size in sizes:
        max_chars = max(4, int(col_width / (size * CHAR_W)))
        lines = _wrap(text, max_chars, max_lines)
        if len(lines) <= max_lines and all(len(ln) * size * CHAR_W <= col_width for ln in lines) \
                and not any(ln.endswith("…") for ln in lines):
            return lines, size
    size = sizes[-1]
    return _wrap(text, max(4, int(col_width / (size * CHAR_W))), max_lines), size


def _block_top(bottom_baseline: int, n_lines: int, size: int) -> int:
    """First baseline so that the last line sits on bottom_baseline (bottom-anchored blocks)."""
    return bottom_baseline - (max(n_lines, 1) - 1) * int(size * 1.08)


def _text_block(lines: list[str], x: int, y: int, size: int, fill: str, weight: str = "700",
                anchor: str = "start") -> str:
    out = []
    for i, line in enumerate(lines):
        out.append(f'<text x="{x}" y="{y + i * int(size * 1.08)}" font-family="{FONT}" '
                   f'font-size="{size}" font-weight="{weight}" fill="{fill}" '
                   f'text-anchor="{anchor}">{html.escape(line)}</text>')
    return "\n".join(out)


def render_poster_svg(spec: dict, product_name: str = "") -> str:
    """Pure function: poster spec -> 1080x1080 SVG. Never fails."""
    pal = spec.get("palette") or {}
    bg = _hex(pal.get("bg", ""), "#141414")
    fg = _hex(pal.get("fg", ""), "#fafafa")
    ac = _hex(pal.get("accent", ""), "#ff5a1f")
    layout = spec.get("layout") if spec.get("layout") in ("stacked", "split", "badge") else "stacked"
    headline = str(spec.get("headline") or "").strip() or product_name or "Ghost Agency"
    subline = str(spec.get("subline") or "").strip()
    glyph = html.escape(str(spec.get("glyph") or "✦").strip()[:2])
    product = html.escape(product_name or "")

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1080" width="1080" height="1080" '
             f'role="img" aria-label="{html.escape(str(spec.get("alt_text") or headline))}">',
             f'<rect width="1080" height="1080" fill="{bg}"/>']

    HEAD = (120, 104, 96, 84, 72, 64)
    SUB = (42, 38, 34, 30)
    if layout == "stacked":
        parts.append(f'<rect x="80" y="80" width="120" height="14" fill="{ac}"/>')
        parts.append(f'<text x="920" y="230" font-size="180" text-anchor="end" font-family="{FONT}">{glyph}</text>')
        hl, hs = _fit(headline, 920, 3, HEAD)
        parts.append(_text_block(hl, 80, _block_top(780, len(hl), hs), hs, fg))
        sl, ss = _fit(subline, 920, 2, SUB)
        parts.append(_text_block(sl, 80, 900, ss, fg, "400"))
    elif layout == "split":
        parts.append(f'<rect x="540" y="0" width="540" height="1080" fill="{ac}"/>')
        parts.append(f'<text x="810" y="600" font-size="260" text-anchor="middle" font-family="{FONT}">{glyph}</text>')
        hl, hs = _fit(headline, 470, 4, HEAD)
        parts.append(_text_block(hl, 70, _block_top(700, len(hl), hs), hs, fg))
        sl, ss = _fit(subline, 470, 3, SUB)
        parts.append(_text_block(sl, 70, _block_top(930, len(sl), ss), ss, fg, "400"))
    else:  # badge
        parts.append(f'<circle cx="540" cy="400" r="280" fill="{ac}"/>')
        parts.append(f'<text x="540" y="470" font-size="210" text-anchor="middle" font-family="{FONT}">{glyph}</text>')
        hl, hs = _fit(headline, 920, 2, (84, 72, 64, 56))
        parts.append(_text_block(hl, 540, _block_top(870, len(hl), hs), hs, fg, "700", "middle"))
        sl, ss = _fit(subline, 920, 2, (34, 30, 26))
        parts.append(_text_block(sl, 540, 870 + 58, ss, fg, "400", "middle"))

    if product:
        parts.append(f'<text x="80" y="1020" font-family="{FONT}" font-size="28" font-weight="500" '
                     f'fill="{fg}" opacity="0.7">{product}</text>')
    parts.append(f'<text x="1000" y="1020" font-family="{FONT}" font-size="22" text-anchor="end" '
                 f'fill="{fg}" opacity="0.5">Ghost Agency</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------ metrics simulation

BASE_IMPRESSIONS = {"x": 1200, "linkedin": 800, "instagram": 1500, "landing": 600}
DECAY = {0: 1.3, 1: 1.0, 2: 0.7}          # days since publish -> attention multiplier; >=3 -> 0.45


def _hook_family(headline: str) -> str | None:
    if re.search(r"\d", headline or ""):
        return "number"
    if (headline or "").strip().endswith("?"):
        return "question"
    return None


def metric_factors(c: dict, day: int, brief: dict, earlier: list[dict], live: list[dict]) -> dict:
    """Explainable multipliers for one published post on one day (deterministic, no randomness)."""
    h = (c.get("headline") or "").strip()
    age = max(0, day - int(c.get("published_day") or 0))
    f = {"decay": DECAY.get(age, 0.45), "hook": 1.0, "channel_fit": 1.0, "fatigue": 1.0,
         "saturation": 1.0, "risk": 1.0, "age_days": age}
    if re.search(r"\d", h):
        f["hook"] *= 1.25
    if len(h) <= 30:
        f["hook"] *= 1.15
    if h.endswith("?"):
        f["hook"] *= 1.1
    if len(h) > 60:
        f["hook"] *= 0.7
    channels = list(brief.get("channels") or [])
    if len(channels) > 1:
        if c.get("channel") == channels[0]:
            f["channel_fit"] = 1.15
        elif c.get("channel") == channels[-1]:
            f["channel_fit"] = 0.9
    first3 = " ".join(h.lower().split()[:3])
    if first3 and any(" ".join((e.get("headline") or "").lower().split()[:3]) == first3 for e in earlier):
        f["fatigue"] = 0.5
    fam = _hook_family(h)
    if fam and sum(1 for x in live if _hook_family(x.get("headline") or "") == fam) >= 2:
        f["saturation"] = 0.75
    if c.get("risk_flags"):
        f["risk"] = 0.9
    return f


def simulate_metrics(brief: dict, day: int, published: list[dict]) -> list[dict]:
    """One day of metrics for each published content item.

    Deterministic seed = brief id + day. Multipliers come from metric_factors: launch bump then
    decay, hook quality, channel fit, fatigue (repeated opening), saturation (same hook family),
    and risk flags (people bounce on unverified claims). Every row carries its factors.
    """
    if not published:
        return []
    brief_id = brief["id"]
    seed = int(hashlib.sha256(f"{brief_id}:{day}".encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    ordered = sorted(published, key=lambda c: (c.get("published_at") or "", c.get("created_at") or ""))
    rows = []
    for i, c in enumerate(ordered):
        f = metric_factors(c, day, brief, ordered[:i], ordered)
        base = BASE_IMPRESSIONS.get(c.get("channel"), 1000)
        impressions = int(base * rng.uniform(0.85, 1.15) * f["decay"] * f["channel_fit"])
        ctr = rng.uniform(0.012, 0.03) * f["hook"] * f["fatigue"] * f["saturation"]
        like_rate = rng.uniform(0.02, 0.05) * f["hook"] * f["fatigue"]
        clicks = int(impressions * ctr)
        likes = int(impressions * like_rate)
        shares = int(likes * rng.uniform(0.05, 0.2))
        signups = int(clicks * rng.uniform(0.1, 0.22) * f["risk"])
        rows.append({"content_id": c["id"], "brief_id": brief_id, "day": day,
                     "impressions": impressions, "clicks": clicks, "likes": likes,
                     "shares": shares, "signups": signups,
                     "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                     "factors": f})
    return rows


# ------------------------------------------------------------------ paid media in metrics

CPM_EUR = {"x": 6.0, "linkedin": 14.0, "instagram": 8.0}


def apply_paid_media(rows: list[dict], published: list[dict], allocation: list[dict] | None,
                     seed_key: str) -> list[dict]:
    """Add paid impressions and spend from an ad plan to a day's organic metric rows (in place)."""
    if not allocation:
        for r in rows:
            r["paid_impressions"], r["ad_spend_eur"] = 0, 0.0
        return rows
    rng = random.Random(int(hashlib.sha256(f"paid:{seed_key}".encode()).hexdigest(), 16) % (2**32))
    by_channel: dict[str, list[dict]] = {}
    for r in rows:
        c = next((p for p in published if p["id"] == r["content_id"]), None)
        if c:
            by_channel.setdefault(c["channel"], []).append(r)
    for r in rows:
        r["paid_impressions"], r["ad_spend_eur"] = 0, 0.0
    for a in allocation:
        posts = by_channel.get(a["channel"])
        daily = float(a.get("daily_eur") or 0)
        if not posts or daily <= 0:
            continue
        share = daily / len(posts)
        for r in posts:
            paid = int(share / CPM_EUR.get(a["channel"], 8.0) * 1000 * rng.uniform(0.85, 1.15))
            organic_ctr = r["clicks"] / r["impressions"] if r["impressions"] else 0.02
            paid_clicks = int(paid * organic_ctr * 0.7)
            r["paid_impressions"] = paid
            r["ad_spend_eur"] = round(share, 2)
            r["impressions"] += paid
            r["clicks"] += paid_clicks
            r["signups"] += int(paid_clicks * rng.uniform(0.08, 0.2))
            r["ctr"] = round(r["clicks"] / r["impressions"], 4) if r["impressions"] else 0.0
    return rows


# ------------------------------------------------------------------ audience comments

_AUTHORS = ["Sanne", "Daan", "Femke", "Joris", "Lotte", "Bram_020", "Noor", "Thijs", "Eva", "Ruben", "Isa", "Kai"]
_COMMENTS = {
    "positive": ["Finally. Lost mine twice this month.", "Ok this is actually clever", "Signed up, this is exactly my problem",
                 "Sharing this with my whole student house", "The tone of this account is perfect lol", "Take my money"],
    "neutral": ["Is this Amsterdam only?", "How fast is the replacement really?", "Do the lights fit a bakfiets?",
                "What happens if I cancel after they replace them?", "Can I get this for my kid's bike too?",
                "When exactly is the launch?"],
    "negative": ["{price} a month for lights? I can buy 3 sets for that", "Feels like a subscription for something that should just work",
                 "Sounds too good, what is the catch", "Another subscription, no thanks", "Replaced within 24h? I doubt it"],
}


def simulate_comments(brief: dict, day: int, published: list[dict], per_post: int = 2) -> list[dict]:
    """Deterministic audience comments for a day; mood skews by each post's engagement rank."""
    if not published:
        return []
    rng = random.Random(int(hashlib.sha256(f"comments:{brief['id']}:{day}".encode()).hexdigest(), 16) % (2**32))
    price = "€9"
    for tok in (brief.get("one_liner") or "").split():
        if "€" in tok or tok.lower().startswith("eur"):
            price = tok.strip(".,")
            break
    out, n = [], 0
    for c in published:
        for _ in range(per_post):
            mood = rng.choices(["positive", "neutral", "negative"], weights=[5, 4, 3])[0]
            text = rng.choice(_COMMENTS[mood]).format(price=price)
            n += 1
            out.append({"brief_id": brief["id"], "content_id": c["id"], "day": day, "label": f"K{n}",
                        "author": rng.choice(_AUTHORS), "channel": c["channel"], "text": text, "mood": mood})
    return out


# ------------------------------------------------------------------ motion (video)

def render_motion_html(spec: dict, product_name: str = "", loop: bool = True) -> str:
    """Storyboard spec -> self-contained animated HTML (720x720). Pure CSS, no JS, never fails."""
    scenes = list(spec.get("scenes") or [])
    if not scenes:
        scenes = [{"text": product_name or "Ghost Agency", "subtext": "", "glyph": "", "bg": "#0b0d12",
                   "fg": "#ffffff", "accent": "#c6ff4a", "seconds": 3, "style": "punch"}]
    scenes = scenes + [{"text": product_name or spec.get("title") or "Ghost Agency", "subtext": "Produced by Ghost Agency",
                        "glyph": "", "bg": "#07090d", "fg": "#ffffff", "accent": "#c6ff4a", "seconds": 2.5, "style": "calm"}]
    total = sum(float(s.get("seconds") or 3) for s in scenes)
    css, divs, t = [], [], 0.0
    for i, s in enumerate(scenes):
        dur = float(s.get("seconds") or 3)
        a, b = t / total * 100, (t + dur) / total * 100
        t += dur
        bg, fg, ac = _hex(s.get("bg", ""), "#0b0d12"), _hex(s.get("fg", ""), "#ffffff"), _hex(s.get("accent", ""), "#c6ff4a")
        style = s.get("style") if s.get("style") in ("punch", "calm", "split") else "punch"
        fade = 0.6 if style == "calm" else 0.15
        fa = min(b, a + fade / total * 100)
        fb = max(fa, b - fade / total * 100)
        css.append(f".s{i}{{animation:sh{i} {total}s {'infinite' if loop else '1'} both;background:{bg};color:{fg}}}"
                   f".s{i} .ac{{background:{ac}}}.s{i} .g{{color:{ac}}}"
                   f"@keyframes sh{i}{{0%,{a:.3f}%{{opacity:0;visibility:hidden}}{fa:.3f}%{{opacity:1;visibility:visible}}"
                   f"{fb:.3f}%{{opacity:1;visibility:visible}}{b:.3f}%,100%{{opacity:0;visibility:hidden}}}}"
                   f".s{i} .txt{{animation:tx{i} {total}s {'infinite' if loop else '1'} both}}"
                   f"@keyframes tx{i}{{0%,{a:.3f}%{{transform:translateY({'22px' if style == 'punch' else '8px'}) scale({'.92' if style == 'punch' else '1'})}}"
                   f"{fa:.3f}%,100%{{transform:none}}}}")
        glyph = html.escape(str(s.get("glyph") or "")[:2])
        text = html.escape(str(s.get("text") or ""))
        sub = html.escape(str(s.get("subtext") or ""))
        if style == "split":
            divs.append(f'<div class="sc s{i} split"><div class="half ac"><div class="g big">{glyph}</div></div>'
                        f'<div class="half"><div class="txt"><div class="t">{text}</div><div class="u">{sub}</div></div></div></div>')
        else:
            divs.append(f'<div class="sc s{i}"><div class="bar ac"></div>{"<div class=g>" + glyph + "</div>" if glyph else ""}'
                        f'<div class="txt"><div class="t">{text}</div><div class="u">{sub}</div></div>'
                        f'<div class="pn">{html.escape(product_name)}</div></div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(spec.get('title') or 'Ghost Agency')}</title>
<style>
html,body{{margin:0;background:#000;width:720px;height:720px;overflow:hidden;font-family:"Space Grotesk","Helvetica Neue",Helvetica,Arial,system-ui,sans-serif}}
.sc{{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:flex-end;padding:56px;box-sizing:border-box}}
.bar{{position:absolute;left:56px;top:56px;width:72px;height:10px}}
.g{{position:absolute;right:56px;top:44px;font-size:120px;line-height:1}}
.t{{font-size:72px;font-weight:700;line-height:.98;letter-spacing:-.03em;word-wrap:break-word}}
.u{{font-size:26px;margin-top:18px;opacity:.85;line-height:1.3}}
.pn{{position:absolute;left:56px;bottom:22px;font-size:16px;opacity:.55}}
.split{{flex-direction:row;padding:0}}.split .half{{flex:1;display:flex;align-items:center;justify-content:center;padding:48px;box-sizing:border-box}}
.split .big{{position:static;font-size:200px}}.split .txt .t{{font-size:60px}}
{''.join(css)}
</style></head><body>{''.join(divs)}</body></html>"""


def motion_duration(spec: dict) -> float:
    return sum(float(s.get("seconds") or 3) for s in (spec.get("scenes") or [])) + 2.5


def record_motion_video(html_path: str, seconds: float, out_path: str) -> str:
    """Record the animation with Chromium (Playwright) to a WebM file. Raises on failure."""
    import shutil
    import tempfile
    from playwright.sync_api import sync_playwright

    tmp = tempfile.mkdtemp(prefix="ghost-video-")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 720, "height": 720},
                                  record_video_dir=tmp, record_video_size={"width": 720, "height": 720})
        page = ctx.new_page()
        page.goto("file://" + os.path.abspath(html_path))
        page.wait_for_timeout(int(seconds * 1000) + 400)
        video = page.video
        ctx.close()
        browser.close()
        src = video.path()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    shutil.move(src, out_path)
    shutil.rmtree(tmp, ignore_errors=True)
    return out_path
