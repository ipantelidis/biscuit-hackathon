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


LEGACY_LAYOUT = {"stacked": "photo", "badge": "frame", "split": "split"}


def _overlay(treatment: str) -> tuple[str, str]:
    """(gradient stop colour, text colour) for a photo layout."""
    return ("#ffffff", "#141414") if treatment == "light" else ("#000000", "#ffffff")


def _brand(product: str, x: int, y: int, fill: str, anchor: str = "start", size: int = 22) -> str:
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="600" letter-spacing="4" '
            f'fill="{fill}" text-anchor="{anchor}">{html.escape(product.upper())}</text>')


def _render_flat(spec: dict, product_name: str) -> str:
    """No photo available: a quiet typographic poster (no emoji)."""
    pal = spec.get("palette") or {}
    bg = _hex(pal.get("bg", ""), "#141414")
    fg = _hex(pal.get("fg", ""), "#fafafa")
    ac = _hex(pal.get("accent", ""), "#c6ff4a")
    headline = str(spec.get("headline") or "").strip() or product_name or "Ghost Boosters"
    subline = str(spec.get("subline") or "").strip()
    parts = [f'<rect width="1080" height="1080" fill="{bg}"/>',
             f'<rect x="0" y="0" width="1080" height="14" fill="{ac}"/>',
             _brand(product_name, 72, 110, fg)]
    hl, hs = _fit(headline, 936, 3, (112, 96, 84, 72, 64))
    parts.append(_text_block(hl, 72, _block_top(760, len(hl), hs), hs, fg))
    sl, ss = _fit(subline, 936, 2, (36, 32, 28))
    parts.append(_text_block(sl, 72, 830, ss, fg, "400"))
    parts.append(f'<rect x="72" y="960" width="120" height="8" fill="{ac}"/>')
    return "\n".join(parts)


def render_poster_svg(spec: dict, product_name: str = "", photo: dict | None = None) -> str:
    """Poster spec (+ optional real photo) -> 1080x1080 SVG. Never fails."""
    pal = spec.get("palette") or {}
    bg = _hex(pal.get("bg", ""), "#141414")
    fg = _hex(pal.get("fg", ""), "#fafafa")
    ac = _hex(pal.get("accent", ""), "#c6ff4a")
    layout = spec.get("layout") or "photo"
    layout = LEGACY_LAYOUT.get(layout, layout)
    if layout not in ("photo", "split", "frame"):
        layout = "photo"
    treatment = spec.get("treatment") if spec.get("treatment") in ("dark", "light") else "dark"
    headline = str(spec.get("headline") or "").strip() or product_name or "Ghost Boosters"
    subline = str(spec.get("subline") or "").strip()
    alt = html.escape(str(spec.get("alt_text") or headline))
    product = product_name or ""

    head = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 1080 1080" '
            f'width="1080" height="1080" role="img" aria-label="{alt}">')
    if not photo or not photo.get("path") or not Path(photo["path"]).exists():
        return head + "\n" + _render_flat(spec, product) + "\n</svg>"
    uri = photo_data_uri(photo["path"])
    grad_col, txt = _overlay(treatment)
    parts = [head, "<defs>",
             f'<linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{grad_col}" stop-opacity="0"/>'
             f'<stop offset="0.55" stop-color="{grad_col}" stop-opacity="0.25"/><stop offset="1" stop-color="{grad_col}" stop-opacity="0.88"/></linearGradient>',
             f'<clipPath id="top"><rect x="0" y="0" width="1080" height="640"/></clipPath>',
             f'<clipPath id="inner"><rect x="56" y="56" width="968" height="700"/></clipPath>',
             "</defs>"]
    if layout == "photo":
        parts.append(f'<image xlink:href="{uri}" x="0" y="0" width="1080" height="1080" preserveAspectRatio="xMidYMid slice"/>')
        parts.append('<rect width="1080" height="1080" fill="url(#g)"/>')
        parts.append(_brand(product, 72, 104, txt))
        hl, hs = _fit(headline, 936, 3, (92, 80, 70, 62, 54))
        top = _block_top(900 if subline else 960, len(hl), hs)
        parts.append(f'<rect x="72" y="{top - hs - 26}" width="72" height="8" fill="{ac}"/>')
        parts.append(_text_block(hl, 72, top, hs, txt))
        if subline:
            sl, ss = _fit(subline, 936, 2, (34, 30, 26))
            parts.append(_text_block(sl, 72, 952, ss, txt, "400"))
    elif layout == "split":
        parts.append(f'<rect width="1080" height="1080" fill="{bg}"/>')
        parts.append(f'<image xlink:href="{uri}" x="0" y="0" width="1080" height="640" preserveAspectRatio="xMidYMid slice" clip-path="url(#top)"/>')
        parts.append(f'<rect x="72" y="700" width="72" height="8" fill="{ac}"/>')
        hl, hs = _fit(headline, 936, 2, (80, 70, 62, 54))
        parts.append(_text_block(hl, 72, 700 + 8 + hs + 16, hs, fg))
        if subline:
            sl, ss = _fit(subline, 936, 2, (32, 28, 26))
            parts.append(_text_block(sl, 72, 700 + 8 + hs + 16 + (len(hl) - 1) * int(hs * 1.08) + 56, ss, fg, "400"))
        parts.append(_brand(product, 1008, 1020, fg, "end"))
    else:  # frame
        parts.append(f'<rect width="1080" height="1080" fill="{bg}"/>')
        parts.append(f'<image xlink:href="{uri}" x="56" y="56" width="968" height="700" preserveAspectRatio="xMidYMid slice" clip-path="url(#inner)"/>')
        hl, hs = _fit(headline, 968, 2, (64, 56, 50, 44))
        parts.append(_text_block(hl, 56, 756 + 24 + hs, hs, fg))
        if subline:
            sl, ss = _fit(subline, 968, 2, (28, 26, 24))
            parts.append(_text_block(sl, 56, 756 + 24 + hs + (len(hl) - 1) * int(hs * 1.08) + 44, ss, fg, "400"))
        parts.append(f'<rect x="56" y="1004" width="72" height="6" fill="{ac}"/>')
        parts.append(_brand(product, 1024, 1012, fg, "end", 18))
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

def render_motion_html(spec: dict, product_name: str = "", loop: bool = True, clip_url=None) -> str:
    """Storyboard spec -> animated HTML (720x720). A scene with a real clip plays it as the
    background; a scene with a photo gets a slow push-in; otherwise a plain colour. `clip_url`
    maps a clip path to the URL the page should load it from (file:// for recording, /api/media
    when served). Never fails."""
    clip_url = clip_url or (lambda path: "file://" + os.path.abspath(path))
    scenes = list(spec.get("scenes") or [])
    if not scenes:
        scenes = [{"text": product_name or "Ghost Boosters", "subtext": "", "bg": "#0b0d12", "fg": "#ffffff",
                   "accent": "#c6ff4a", "seconds": 3, "style": "punch"}]
    scenes = scenes + [{"text": product_name or spec.get("title") or "Ghost Boosters", "subtext": "Produced by Ghost Boosters",
                        "bg": "#07090d", "fg": "#ffffff", "accent": "#c6ff4a", "seconds": 2.5, "style": "calm", "photo": None}]
    total = sum(float(s.get("seconds") or 3) for s in scenes)
    css, divs, t = [], [], 0.0
    it = "infinite" if loop else "1"
    for i, s in enumerate(scenes):
        dur = float(s.get("seconds") or 3)
        a, b = t / total * 100, (t + dur) / total * 100
        t += dur
        bg, fg, ac = _hex(s.get("bg", ""), "#0b0d12"), _hex(s.get("fg", ""), "#ffffff"), _hex(s.get("accent", ""), "#c6ff4a")
        style = s.get("style") if s.get("style") in ("punch", "calm", "split") else "punch"
        fade = 0.6 if style == "calm" else 0.2
        fa = min(b, a + fade / total * 100)
        fb = max(fa, b - fade / total * 100)
        photo = s.get("photo")
        clip = s.get("clip")
        clip = clip if clip and Path(str(clip)).exists() else None
        uri = photo_data_uri(photo) if (photo and not clip and Path(str(photo)).exists()) else None
        treatment = s.get("treatment") if s.get("treatment") in ("dark", "light") else "dark"
        txt = "#141414" if ((uri or clip) and treatment == "light") else (fg if not (uri or clip) else "#ffffff")
        grad = "255,255,255" if treatment == "light" else "0,0,0"
        css.append(f".s{i}{{animation:sh{i} {total}s {it} both;background:{bg};color:{txt}}}.s{i} .ac{{background:{ac}}}"
                   f"@keyframes sh{i}{{0%,{a:.3f}%{{opacity:0;visibility:hidden}}{fa:.3f}%{{opacity:1;visibility:visible}}"
                   f"{fb:.3f}%{{opacity:1;visibility:visible}}{b:.3f}%,100%{{opacity:0;visibility:hidden}}}}"
                   f".s{i} .txt{{animation:tx{i} {total}s {it} both}}"
                   f"@keyframes tx{i}{{0%,{a:.3f}%{{transform:translateY({'22px' if style == 'punch' else '8px'})}}{fa:.3f}%,100%{{transform:none}}}}")
        if uri:
            css.append(f".s{i} .bg{{background-image:url({uri});animation:kb{i} {total}s {it} both}}"
                       f"@keyframes kb{i}{{0%,{a:.3f}%{{transform:scale(1)}}{b:.3f}%,100%{{transform:scale(1.1)}}}}")
        if uri or clip:
            css.append(f".s{i} .ov{{background:linear-gradient(180deg,rgba({grad},0) 30%,rgba({grad},.85) 100%)}}")
        text = html.escape(str(s.get("text") or ""))
        sub = html.escape(str(s.get("subtext") or ""))
        bgdiv = (f'<video class="bg" src="{html.escape(clip_url(clip))}" autoplay muted loop playsinline></video><div class="ov"></div>' if clip
                 else "<div class=bg></div><div class=ov></div>" if uri else "")
        divs.append(f'<div class="sc s{i}">{bgdiv}<div class="bar ac"></div>'
                    f'<div class="txt"><div class="t">{text}</div><div class="u">{sub}</div></div>'
                    f'<div class="pn">{html.escape(product_name)}</div></div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(spec.get('title') or 'Ghost Boosters')}</title>
<style>
html,body{{margin:0;background:#000;width:720px;height:720px;overflow:hidden;font-family:"Helvetica Neue",Helvetica,Arial,system-ui,sans-serif}}
.sc{{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:flex-end;padding:56px;box-sizing:border-box;overflow:hidden}}
.bg{{position:absolute;inset:0;background-size:cover;background-position:center;transform-origin:center}}
video.bg{{width:100%;height:100%;object-fit:cover}}
.ov{{position:absolute;inset:0}}
.bar{{position:absolute;left:56px;top:56px;width:64px;height:8px}}
.txt{{position:relative}}
.t{{font-size:64px;font-weight:700;line-height:1;letter-spacing:-.02em;word-wrap:break-word;text-shadow:0 2px 24px rgba(0,0,0,.25)}}
.u{{font-size:24px;margin-top:16px;opacity:.9;line-height:1.3}}
.pn{{position:absolute;left:56px;top:78px;font-size:14px;letter-spacing:4px;text-transform:uppercase;opacity:.8}}
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


# ------------------------------------------------------------------ real photography

STOPWORDS = {"the", "a", "an", "of", "in", "at", "on", "with", "and", "dark", "light", "bright", "close-up", "closeup",
             "shot", "photo", "image", "picture", "view", "scene", "background"}


def _overlap(query: str, text: str) -> int:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in STOPWORDS]
    text = text.lower()
    return sum(1 for w in words if w in text)


def _on_subject(text: str, subject: list[str] | None) -> bool:
    """True when no subject list is given, or the description contains one of the subject words."""
    if not subject:
        return True
    text = (text or "").lower()
    return any(w.lower().strip() in text for w in subject if w and len(w.strip()) > 2)

PHOTO_DIR = Path(__file__).parent / "media" / "photos"


def _photo_cache_key(query: str, kind: str = "photo") -> str:
    prov = "pexels" if os.environ.get("PEXELS_API_KEY", "").strip() else "wiki"
    return hashlib.sha1(f"{kind}:{prov}:{query.strip().lower()}".encode()).hexdigest()[:16]


def _pexels_search(query: str, key: str, subject: list[str] | None = None) -> dict | None:
    r = httpx.get("https://api.pexels.com/v1/search", params={"query": query, "per_page": 10, "orientation": "square", "size": "large"},
                  headers={"Authorization": key}, timeout=15)
    r.raise_for_status()
    photos = r.json().get("photos") or []
    if not photos:
        r = httpx.get("https://api.pexels.com/v1/search", params={"query": query, "per_page": 6}, headers={"Authorization": key}, timeout=15)
        r.raise_for_status()
        photos = r.json().get("photos") or []
    photos = [p for p in photos if _on_subject((p.get("alt") or "") + " " + (p.get("url") or ""), subject)]
    photos.sort(key=lambda p: -_overlap(query, (p.get("alt") or "") + " " + (p.get("url") or "")))
    return [{"url": p["src"].get("large2x") or p["src"]["large"], "credit": f"{p.get('photographer', 'Pexels')} / Pexels",
             "source_url": p.get("url", ""), "provider": "pexels"} for p in photos] or None


def _wikimedia_search(query: str, subject: list[str] | None = None) -> dict | None:
    for attempt in range(3):
        r = _wikimedia_get(query)
        if r.status_code != 429:
            break
        time.sleep(2.5 * (attempt + 1))  # Commons rate-limits bursts; back off and retry
    r.raise_for_status()
    return _wikimedia_pick(query, r, subject)


def _wikimedia_get(query: str):
    return httpx.get("https://commons.wikimedia.org/w/api.php",
                  params={"action": "query", "generator": "search", "gsrsearch": f"{query} filemime:image/jpeg", "gsrnamespace": 6,
                          "gsrlimit": 8, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1400, "format": "json"},
                  headers={"User-Agent": "GhostAgency/1.0 (hackathon demo)"}, timeout=20)


def _wikimedia_pick(query: str, r, subject: list[str] | None = None) -> dict | None:
    pages = list((r.json().get("query") or {}).get("pages", {}).values())
    pages = [p for p in pages if p.get("imageinfo") and (p["imageinfo"][0].get("width") or 0) >= 1000]
    if subject:
        def _txt(pg):
            meta = pg["imageinfo"][0].get("extmetadata") or {}
            return pg.get("title", "") + " " + (meta.get("ImageDescription") or {}).get("value", "") + " " + (meta.get("Categories") or {}).get("value", "")
        pages = [p for p in pages if _on_subject(_txt(p), subject)]
    if not pages:
        return None
    words = [w for w in query.lower().split() if len(w) > 2]

    def score(pg):
        info = pg["imageinfo"][0]
        meta = info.get("extmetadata") or {}
        text = (pg.get("title", "") + " " + re.sub(r"<[^>]+>", "", (meta.get("ImageDescription") or {}).get("value", ""))
                + " " + (meta.get("Categories") or {}).get("value", "")).lower()
        hits = sum(1 for w in words if w in text)
        aspect = info.get("width", 1) / max(1, info.get("height", 1))
        return (hits, -abs(aspect - 1.0), info.get("width", 0))  # relevance, then squareness, then size
    pages.sort(key=score, reverse=True)
    if words and score(pages[0])[0] == 0 and len(words) > 1:
        return None  # nothing mentions the subject; let the caller relax the query
    out = []
    for pg in pages[:5]:
        p = pg["imageinfo"][0]
        meta = p.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:40]
        out.append({"url": p.get("thumburl") or p["url"], "credit": f"{artist or 'Wikimedia Commons'} / Wikimedia",
                    "source_url": p.get("descriptionurl", ""), "provider": "wikimedia"})
    return out


def _download(url: str, dest_dir: Path) -> Path | None:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    for ext in (".jpg", ".png", ".mp4"):
        if (dest_dir / f"{name}{ext}").exists():
            return dest_dir / f"{name}{ext}"
    r = httpx.get(url, timeout=90, follow_redirects=True, headers={"User-Agent": "GhostAgency/1.0 (hackathon demo)"})
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if not (ct.startswith("image/") or ct.startswith("video/")):
        return None
    ext = ".png" if "png" in ct else ".mp4" if ct.startswith("video/") else ".jpg"
    path = dest_dir / f"{name}{ext}"
    path.write_bytes(r.content)
    return path


def find_photo(query: str, exclude: set[str] | None = None, subject: list[str] | None = None) -> dict | None:
    """Real photo for a query -> {path, credit, source_url, provider, url}; candidates cached on
    disk, files cached by URL. `exclude` = photo URLs already used elsewhere in the campaign."""
    query = " ".join((query or "").split())[:80]
    if not query or env_flag("NO_PHOTOS"):
        return None
    exclude = exclude or set()
    subject = [w.strip().lower() for w in (subject or []) if w and w.strip()]
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    key = _photo_cache_key(query + ("|" + ",".join(sorted(subject)) if subject else ""))
    cand_path = PHOTO_DIR / f"{key}.json"
    candidates: list[dict] | None = None
    if cand_path.exists():
        candidates = json.loads(cand_path.read_text(encoding="utf-8"))
    if candidates is None:
        providers = []
        pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
        if pexels_key:
            providers.append(lambda: _pexels_search(query, pexels_key, subject))
            for sw in subject[:2]:  # on-subject but plainer queries before giving up
                providers.append(lambda q=f"{sw} {query.split()[-1]}" if query.split() else sw: _pexels_search(q, pexels_key, subject))
        words = query.split()
        for n in range(len(words), max(0, min(2, len(words)) - 1), -1):  # keyless fallback: relax to at least 2 words
            providers.append(lambda q=" ".join(words[:n]): _wikimedia_search(q, subject))
        if len(words) > 1:
            providers.append(lambda q=words[0]: _wikimedia_search(q, subject))  # last resort: the subject alone
        for prov in providers:
            try:
                hits = prov()
                if hits:
                    candidates = hits
                    break
            except Exception as e:  # next provider
                log.warning("photo provider failed for %r: %s", query, e)
        if candidates is None:
            return None
        cand_path.write_text(json.dumps(candidates), encoding="utf-8")
    ordered = [c for c in candidates if c["url"] not in exclude] or candidates
    for c in ordered:
        try:
            path = _download(c["url"], PHOTO_DIR)
            if path:
                return {**c, "path": str(path), "query": query}
        except Exception as e:
            log.warning("photo download failed: %s", e)
    return None


def photo_data_uri(path: str) -> str:
    import base64
    p = Path(path)
    mime = "image/png" if p.suffix == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


# ------------------------------------------------------------------ real footage (Pexels Videos)

CLIP_DIR = Path(__file__).parent / "media" / "clips"


def find_video(query: str, max_seconds: int = 30, exclude: set[str] | None = None, subject: list[str] | None = None) -> dict | None:
    """Real licensed clip for a query -> {path, credit, source_url, duration, provider}; cached. Pexels only."""
    query = " ".join((query or "").split())[:80]
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not query or not key or env_flag("NO_PHOTOS"):
        return None
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    subject = [w.strip().lower() for w in (subject or []) if w and w.strip()]
    ck = _photo_cache_key(query + ("|" + ",".join(sorted(subject)) if subject else ""), "clip")
    meta_path = CLIP_DIR / f"{ck}.json"
    if meta_path.exists() and not exclude:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if Path(meta["path"]).exists():
            return meta
    try:
        exclude = exclude or set()
        words = query.split()
        tries = [" ".join(words[:n]) for n in range(len(words), 1, -1)] or [query]
        tries += [f"{sw} {words[-1]}" if words else sw for sw in subject[:2]] + subject[:2]
        best, best_score = None, -1
        for q in tries:
            for orientation in ("square", "landscape"):
                r = httpx.get("https://api.pexels.com/videos/search",
                              params={"query": q, "per_page": 15, "orientation": orientation, "size": "medium"},
                              headers={"Authorization": key}, timeout=20)
                r.raise_for_status()
                vids = [v for v in r.json().get("videos", []) if 3 <= (v.get("duration") or 0) <= max_seconds
                        and v.get("url") not in exclude]
                for v in vids:  # the URL slug describes the clip: "…/video/man-riding-a-bicycle-at-night-1234/"
                    if not _on_subject(v.get("url", ""), subject):
                        continue
                    sc = _overlap(query, v.get("url", "")) + (1 if subject else 0)
                    if sc > best_score:
                        best, best_score = v, sc
                if best_score >= 2:
                    break
            if best_score >= 2:
                break
        if best is None or best_score < 1:
            return None  # nothing on topic: the caller falls back to a ranked photo rather than a random clip
        v = best
        files = [f for f in v["video_files"] if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 640]
        files.sort(key=lambda f: abs((f.get("width") or 0) - 960))  # ~960 px: sharp enough, small enough
        f = files[0] if files else v["video_files"][0]
        path = _download(f["link"], CLIP_DIR)
        if not path:
            return None
        meta = {"path": str(path), "credit": f"{v.get('user', {}).get('name', 'Pexels')} / Pexels", "source_url": v.get("url", ""),
                "duration": v.get("duration"), "provider": "pexels", "query": query,
                "width": f.get("width"), "height": f.get("height")}
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return meta
    except Exception as e:
        log.warning("footage lookup failed for %r: %s", query, e)
        return None


# ------------------------------------------------------------------ generated video (local GPU)

GEN_DIR = Path(__file__).parent / "media" / "generated"
VIDEO_VENV = Path(__file__).parent / ".venv-video" / "bin" / "python"


def video_generation_available() -> bool:
    """VIDEO_GEN=0 disables; otherwise on when the video environment exists."""
    if os.environ.get("VIDEO_GEN", "auto").strip().lower() in ("0", "false", "off", "no"):
        return False
    return VIDEO_VENV.exists()


def visible_gpus() -> list[int]:
    raw = os.environ.get("VIDEO_GPUS", "").strip()
    if raw:
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]
    try:
        import subprocess
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        free = [int(l.split(",")[0]) for l in out.strip().splitlines() if int(l.split(",")[1]) < 4000]
        return free or [0]
    except Exception:
        return [0]


def generate_clip(prompt: str, out_path: str, seconds: float = 4.0, gpu: int = 0, seed: int = 7) -> dict:
    """Run video_gen.py in the video environment on one GPU. Returns its JSON result."""
    import subprocess
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": str(gpu)}
    cmd = [str(VIDEO_VENV), str(Path(__file__).parent / "video_gen.py"), "--prompt", prompt, "--out", out_path,
           "--seconds", str(seconds), "--seed", str(seed)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=float(os.environ.get("VIDEO_GEN_TIMEOUT", "900")), env=env)
        last = (r.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            res = json.loads(last)
        except ValueError:
            res = {"ok": False, "error": (r.stderr or r.stdout)[-300:]}
        if not res.get("ok"):
            log.warning("clip generation failed on gpu %s: %s", gpu, res.get("error"))
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def scene_prompt(sc: dict, brief: dict) -> str:
    base = sc.get("video_prompt") or sc.get("photo_query") or sc.get("text") or ""
    return (f"{base}. Realistic cinematic footage, natural light, handheld camera, shallow depth of field, "
            f"24fps, no text, no logos. Setting: the world of {brief.get('product_name', 'the product')}: "
            f"{(brief.get('one_liner') or '')[:120]}")
