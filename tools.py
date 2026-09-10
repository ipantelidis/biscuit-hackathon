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
        candidates.append(MOCK_DIR / f"{agent_key}_{variant}.json")
    candidates.append(MOCK_DIR / f"{agent_key}.json")
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
    """Tavily search -> [{title,url,snippet}]. Mock or missing key -> canned / empty."""
    if env_flag("MOCK_SEARCH"):
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

    if layout == "stacked":
        parts.append(f'<rect x="80" y="80" width="120" height="14" fill="{ac}"/>')
        parts.append(f'<text x="920" y="230" font-size="180" text-anchor="end" font-family="{FONT}">{glyph}</text>')
        parts.append(_text_block(_wrap(headline, 14, 3), 80, 520, 120, fg))
        parts.append(_text_block(_wrap(subline, 34, 2), 80, 900, 42, fg, "400"))
    elif layout == "split":
        parts.append(f'<rect x="540" y="0" width="540" height="1080" fill="{ac}"/>')
        parts.append(f'<text x="810" y="600" font-size="260" text-anchor="middle" font-family="{FONT}">{glyph}</text>')
        parts.append(_text_block(_wrap(headline, 11, 4), 70, 380, 96, fg))
        parts.append(_text_block(_wrap(subline, 24, 3), 70, 860, 38, fg, "400"))
    else:  # badge
        parts.append(f'<circle cx="540" cy="400" r="280" fill="{ac}"/>')
        parts.append(f'<text x="540" y="470" font-size="210" text-anchor="middle" font-family="{FONT}">{glyph}</text>')
        head_lines = _wrap(headline, 18, 2)
        parts.append(_text_block(head_lines, 540, 790, 80, fg, "700", "middle"))
        sub_y = 790 + (len(head_lines) - 1) * 86 + 58
        parts.append(_text_block(_wrap(subline, 44, 2), 540, sub_y, 34, fg, "400", "middle"))

    if product:
        parts.append(f'<text x="80" y="1020" font-family="{FONT}" font-size="28" font-weight="500" '
                     f'fill="{fg}" opacity="0.7">{product}</text>')
    parts.append(f'<text x="1000" y="1020" font-family="{FONT}" font-size="22" text-anchor="end" '
                 f'fill="{fg}" opacity="0.5">Ghost Agency</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ------------------------------------------------------------------ metrics simulation

BASE_IMPRESSIONS = {"x": 1200, "linkedin": 800, "instagram": 1500, "landing": 600}


def simulate_metrics(brief_id: str, day: int, published: list[dict]) -> list[dict]:
    """One day of metrics for each published content item.

    Deterministic seed = brief_id + day. A clear winner (shortest headline or one containing
    a number) gets x1.8 engagement; a clear loser (longest headline) gets x0.4.
    """
    if not published:
        return []
    seed = int(hashlib.sha256(f"{brief_id}:{day}".encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)

    def score(c):
        h = c.get("headline") or ""
        return (0 if re.search(r"\d", h) else 1, len(h))

    ordered = sorted(published, key=score)
    winner = ordered[0]["id"]
    loser = ordered[-1]["id"] if len(ordered) > 1 and ordered[-1]["id"] != winner else None

    rows = []
    for c in published:
        base = BASE_IMPRESSIONS.get(c.get("channel"), 1000)
        impressions = int(base * rng.uniform(0.75, 1.25))
        ctr = rng.uniform(0.01, 0.04)
        like_rate = rng.uniform(0.02, 0.06)
        mult = 1.8 if c["id"] == winner else 0.4 if c["id"] == loser else 1.0
        clicks = int(impressions * ctr * mult)
        likes = int(impressions * like_rate * mult)
        shares = int(likes * rng.uniform(0.05, 0.2))
        signups = int(clicks * rng.uniform(0.08, 0.2))
        rows.append({"content_id": c["id"], "brief_id": brief_id, "day": day,
                     "impressions": impressions, "clicks": clicks, "likes": likes,
                     "shares": shares, "signups": signups,
                     "ctr": round(clicks / impressions, 4) if impressions else 0.0})
    return rows
