# Ghost Agency — Technical Design Document

**A marketing agency with zero employees.** A client submits a product brief; a team of AI agents researches, strategizes, writes, designs, publishes, measures and iterates on its own. Humans sit only on the board: they see everything, approve or veto, and can pull the plug.

Target: built from scratch in ~3 hours by a team of 3, demoed live in 5 minutes.
This document is written so it can be handed to Claude Code as the single source of truth.

---

## 1. Goals, non-goals, and the demo we're building toward

### 1.1 What must be true at demo time
1. A brief pasted into the dashboard causes agents to start working within seconds, visibly.
2. Agents communicate through a message bus that is shown live on screen.
3. Content lands in an approval queue; a human clicks Approve / Veto.
4. Approved content appears on a public campaign page with visuals.
5. A "Simulate day" button generates metrics; the Analyst reads them, writes a report, and triggers a strategy revision that produces new content — **the loop closes without human prompting**.
6. A spend meter shows cost vs. budget; a kill switch pauses the company.

### 1.2 Non-goals (for the hackathon)
- Real posting to X/LinkedIn/Instagram (stub it; optional stretch).
- Auth, multi-tenancy, billing.
- Real ad spend or real analytics integrations.
- Perfect prompt quality. Short, sharp, plausible output beats long, perfect output.

### 1.3 Design principles
- **One generic agent runner, eight configurations.** Agents are rows in a table (name, role, system prompt, output schema), not eight code paths.
- **The database is the shared brain.** Agents never talk to each other directly; they read and write tables. This makes everything observable and restartable.
- **No agent framework.** A dependency-resolving task loop is ~40 lines and is far easier to debug live than CrewAI/LangGraph/AutoGen.
- **Every LLM call returns strict JSON** validated against a schema, with one retry on failure.
- **Mock mode.** Every external call (LLM, search, image) has a deterministic fake so the whole system runs without keys or network. This is your demo insurance.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Whole team can read it; fastest path to a running loop |
| API server | FastAPI + Uvicorn | Async, auto-docs at `/docs`, trivial static file serving |
| Database | SQLite via `sqlite3` (stdlib) or SQLModel | Zero setup, single file, easy to reset between demo runs. (Swap to Supabase/Postgres only if someone already has an account and wants persistence across machines.) |
| LLM | Anthropic Claude API (`anthropic` SDK). Model via env var `LLM_MODEL`, default to the current Sonnet-class model; check docs.claude.com on the day for the exact string | Strong JSON adherence, fast, cheap enough for ~50 calls per demo |
| Web search | Tavily API (single REST call) — fallback: none (Researcher works from model knowledge and flags it) | Simplest search API; free tier is plenty |
| Images | **Locally rendered SVG posters** generated from Designer output (headline, palette, layout variant) — optional stretch: an image API | Zero latency, zero failure modes, looks intentional. Do not gamble a live demo on an image API |
| Dashboard | Single `dashboard.html` (vanilla JS + CSS, no build step), polls `/api/state` every 2s | No bundler, no npm, anyone can edit it. Consider Streamlit only if the team is more comfortable with it |
| Campaign page | Server-rendered HTML via Jinja2 at `/campaign/{id}` | It's the "client deliverable" |
| Background work | A single orchestrator thread started at app startup (`threading.Thread`, daemon) | Keeps everything in one process; no Celery/Redis |
| Config | `.env` + `python-dotenv` | |
| Repo | Single repo, single process, `uvicorn main:app --reload` | |

**Dependencies:** `fastapi uvicorn anthropic python-dotenv httpx jinja2 pydantic`

**Environment variables:**
```
ANTHROPIC_API_KEY=
LLM_MODEL=            # e.g. current Sonnet model id
TAVILY_API_KEY=       # optional
MOCK_LLM=0            # 1 = deterministic fake outputs, no API calls
MOCK_SEARCH=0
BUDGET_EUR=5.00       # company budget for the demo
PRICE_IN_PER_MTOK=3.0 # € per million input tokens (adjust to model)
PRICE_OUT_PER_MTOK=15.0
TICK_SECONDS=1.5      # orchestrator loop interval
```

---

## 3. System architecture

```
┌──────────────────────────┐          ┌──────────────────────────────┐
│  Board dashboard (HTML)  │ ◀─poll── │  FastAPI                     │
│  org chart · bus feed    │ ──POST─▶ │  /api/*  /campaign/{id}      │
│  approvals · spend · kill│          └───────────┬──────────────────┘
└──────────────────────────┘                      │ read/write
                                                  ▼
                                    ┌──────────────────────────┐
                                    │  SQLite (ghost.db)       │
                                    │  briefs tasks messages   │
                                    │  content assets metrics  │
                                    │  approvals spend company │
                                    └───────────┬──────────────┘
                                                │ read/write
                                                ▼
              ┌──────────────────────────────────────────────────────┐
              │  Orchestrator thread (tick every 1.5s)               │
              │  1. if company.paused → sleep                        │
              │  2. find tasks with status=ready & deps done         │
              │  3. run_agent(task) for each (sequential is fine)    │
              │  4. post-processing hooks per agent type             │
              └───────────┬───────────────────────┬──────────────────┘
                          │                       │
                          ▼                       ▼
                 ┌────────────────┐      ┌─────────────────────┐
                 │ LLM (Claude)   │      │ Tools               │
                 │ system prompt  │      │ web_search()        │
                 │ from agents tbl│      │ render_poster_svg() │
                 └────────────────┘      │ publish()           │
                                         │ simulate_metrics()  │
                                         └─────────────────────┘
```

**Vocabulary for the pitch:** the tables + tick loop are the company's nervous system (A2A-style messaging via the `messages` table); `tools.py` is the hands (MCP-style tool access). You built both by hand; say so.

---

## 4. Data model

All tables have `id` (TEXT, uuid4), `created_at`, `updated_at` (ISO strings). Store JSON in TEXT columns; SQLite's `json_extract` is enough.

### 4.1 `company`  (single row)
| column | type | notes |
|---|---|---|
| paused | INTEGER 0/1 | kill switch |
| budget_eur | REAL | from env |
| spent_eur | REAL | running total |
| name | TEXT | "Ghost Agency" |

### 4.2 `agents`
| column | notes |
|---|---|
| key | `ceo`, `researcher`, `strategist`, `copywriter`, `designer`, `publisher`, `analyst`, `cfo` |
| display_name | e.g. "Nora (Researcher)" — giving them names makes the org chart feel alive |
| role_summary | one line for the org chart |
| system_prompt | full prompt (section 6) |
| output_schema | JSON schema string the LLM must satisfy |
| status | `idle` \| `working` \| `blocked` \| `paused` |
| current_task_id | nullable |
| tokens_in, tokens_out, cost_eur | per-agent accounting |

### 4.3 `briefs`
| column | notes |
|---|---|
| product_name, one_liner, description | |
| audience | free text |
| goals | free text ("100 waitlist signups in 2 weeks") |
| budget_eur | client budget (informational) |
| tone | e.g. "playful, Dutch-direct" |
| channels | JSON list, e.g. `["instagram","linkedin","x"]` |
| status | `new` → `in_progress` → `live` → `iterating` |

### 4.4 `tasks`
| column | notes |
|---|---|
| brief_id | FK |
| agent_key | who does it |
| title | short, human-readable, shown in UI |
| input | JSON: whatever the agent needs (usually references to other tasks' outputs) |
| output | JSON: validated agent result |
| depends_on | JSON list of task ids |
| status | `blocked` \| `ready` \| `running` \| `done` \| `failed` \| `waiting_human` |
| attempt | INTEGER (max 2) |
| error | TEXT |
| round | INTEGER — 1 for initial campaign, 2+ for Analyst-triggered revisions |

**Status rules:** a task is `ready` when every id in `depends_on` has status `done`. The orchestrator recomputes this each tick (cheap; tasks are few).

### 4.5 `messages`  (the bus)
| column | notes |
|---|---|
| brief_id | |
| from_agent, to_agent | agent keys, or `board`, or `all` |
| kind | `handoff` \| `status` \| `question` \| `report` \| `alert` |
| body | ≤ 280 chars, written by the agent in first person (this is what people read on screen) |
| task_id | nullable |

Every agent output schema includes a `message_to_team` field. The runner writes it here automatically. Board actions (approve/veto/pause) also write messages `from_agent='board'`.

### 4.6 `content`
| column | notes |
|---|---|
| brief_id, task_id | |
| channel | instagram / linkedin / x / landing |
| headline | ≤ 60 chars |
| body | post text |
| cta | call to action |
| hashtags | JSON list |
| rationale | why this post exists (shown to board) |
| risk_flags | JSON list from Copywriter's self-check (claims, comparisons, legal) |
| status | `draft` → `pending_approval` → `approved` \| `vetoed` → `published` |
| asset_id | FK to assets (nullable until Designer runs) |
| published_at | |
| round | |

### 4.7 `assets`
| column | notes |
|---|---|
| content_id | |
| kind | `svg_poster` (default) or `image_url` |
| spec | JSON from Designer: headline, subline, palette, layout variant, emoji/glyph |
| svg | rendered SVG string |
| alt_text | |

### 4.8 `metrics`
| column | notes |
|---|---|
| content_id, brief_id | |
| day | INTEGER |
| impressions, clicks, likes, shares, signups | INTEGER |
| ctr | REAL (derived) |

### 4.9 `approvals`
| column | notes |
|---|---|
| content_id | |
| requested_by | `copywriter` |
| decision | null \| `approved` \| `vetoed` |
| decided_by | `board` |
| note | board's optional comment (fed back to Copywriter if vetoed) |

### 4.10 `spend`
| column | notes |
|---|---|
| agent_key, task_id | |
| tokens_in, tokens_out, cost_eur | |

---

## 5. The agent runtime

### 5.1 `run_agent(task)` — one function for all agents
1. Load agent row by `task.agent_key`. Set `agents.status='working'`, `current_task_id`.
2. Build the **user message** from:
   - the brief (always),
   - `task.input`,
   - outputs of dependency tasks (fetched by id, included as labeled JSON blocks),
   - the last 10 messages on the bus for this brief (gives agents shared context and makes handoffs coherent),
   - for Copywriter revisions: veto notes and Analyst recommendations.
3. Call the LLM with the agent's `system_prompt`, plus a suffix: *"Respond with a single JSON object matching this schema and nothing else:"* + `output_schema`. `max_tokens` ≈ 1500. Temperature default.
4. Parse: strip code fences, `json.loads`, validate required keys (Pydantic models, one per agent). On failure → retry once with the error message appended. On second failure → `tasks.status='failed'`, agent `blocked`, alert on bus.
5. Record spend: tokens × price → `spend` table, agent totals, `company.spent_eur`. If `spent_eur > budget_eur` → CFO check fires (section 5.4).
6. Save `task.output`, `status='done'`, agent `idle`.
7. Write `output.message_to_team` to the bus.
8. Run the **post-processing hook** for that agent type (section 5.3).

### 5.2 Orchestrator loop (daemon thread)
```
loop forever:
  sleep TICK_SECONDS
  if company.paused: continue
  for task in tasks where status in (blocked, ready):
      if all deps done: status = ready
  for task in tasks where status == ready (oldest first):
      if task.agent_key == 'publisher' and not publisher_gate_open(task): continue
      run_agent(task)     # sequential; ~5–15s per agent; fine for demo
```
`publisher_gate_open`: all content for this brief & round has `status in (approved, vetoed)` and at least one is approved. Otherwise Publisher stays `ready` but doesn't run (UI shows it as "waiting for board").

Sequential execution is deliberate: the audience can follow one agent at a time. If you want parallelism for effect, run Researcher-independent tasks in a `ThreadPoolExecutor(max_workers=3)` — but only after the sequential version works.

### 5.3 Post-processing hooks
| agent | hook |
|---|---|
| **ceo** | Create task rows from `output.tasks` (mapping CEO's local ids to real uuids for `depends_on`). Set brief `in_progress`. |
| **researcher** | none (output consumed by Strategist) |
| **strategist** | none (output consumed by Copywriter) |
| **copywriter** | Insert one `content` row per item with `status='pending_approval'`; insert `approvals` rows; create one **designer** task per content item (input: content id) — or one Designer task for all items to save calls. Set the Copywriter task's message kind to `handoff`. |
| **designer** | For each item in output: render SVG via `render_poster_svg(spec)`, insert `assets`, link `content.asset_id`. |
| **publisher** | For each approved content: `status='published'`, `published_at=now`. Set brief `live`. Post a bus message with the campaign URL. |
| **analyst** | Save report (`output.report`) as a message of kind `report`. Create a round N+1 chain: **strategist** (input: report + recommendations) → **copywriter** (depends on strategist) → **designer** → **publisher**. Set brief `iterating`. |
| **cfo** | Save memo as bus message. If `output.action == 'pause'` → set `company.paused=1`. |

### 5.4 Budget guard (code-level, not LLM)
After every spend write: if `spent_eur >= 0.8 * budget_eur` and no CFO task exists for this threshold → create a **cfo** task (input: spend breakdown). If `spent_eur >= budget_eur` → pause the company immediately and alert. This guarantees the spend meter matters even if the CFO agent is slow.

### 5.5 Failure handling
- LLM error / timeout: retry once after 2s; then task `failed`. UI shows a red badge and a "Retry" button (`POST /api/tasks/{id}/retry` resets to `ready`).
- JSON invalid: retry with error appended (see 5.1).
- Search unavailable: Researcher proceeds with `sources: []` and `confidence: "low"`; bus message says so.
- Mock mode: `MOCK_LLM=1` makes `call_llm(agent_key, ...)` return canned JSON from `mocks/{agent_key}.json` (write these for all 8 agents — they double as examples for prompt tuning). The full demo must run end to end in mock mode.

---

## 6. Agent specifications

Each agent: **mission**, **inputs**, **output schema**, **prompt guidance**. All schemas include:
```json
"message_to_team": "string, ≤280 chars, first person, addressed to the next agent or the team"
```
Prompts should be short (150–300 words), state the company context ("You work at Ghost Agency, a marketing agency run entirely by AI agents. Humans on the board review your work."), demand brevity, and forbid markdown in JSON strings.

### 6.1 CEO — "Iris"
- **Mission:** turn a brief into a task graph and delegate.
- **Inputs:** brief.
- **Output:**
```json
{
  "campaign_name": "string",
  "objective": "string, one sentence",
  "tasks": [
    {"id": "t1", "agent": "researcher", "title": "string", "input": {...}, "depends_on": []},
    {"id": "t2", "agent": "strategist", "title": "string", "input": {}, "depends_on": ["t1"]},
    {"id": "t3", "agent": "copywriter", "title": "string", "input": {"num_posts": 3}, "depends_on": ["t2"]},
    {"id": "t4", "agent": "publisher", "title": "string", "input": {}, "depends_on": ["t3"]}
  ],
  "escalations": ["string — things the board should know now"],
  "message_to_team": "string"
}
```
- **Guidance:** always produce exactly this 4-task shape for round 1 (constrain it; creativity here only causes bugs). Allowed agents: researcher, strategist, copywriter, publisher. The runtime adds designer tasks itself. `num_posts` between 3 and 4 for demo speed.

### 6.2 Researcher — "Nora"
- **Mission:** market, competitors, audience.
- **Inputs:** brief. Tool: `web_search(query)` — the runtime performs 2–3 searches *before* the LLM call using queries derived from the brief (product category + "competitors", + "target audience", + city/market), and passes results as context. (Simpler than tool-calling loops; same effect.)
- **Output:**
```json
{
  "market_summary": "string ≤120 words",
  "competitors": [{"name": "", "positioning": "", "weakness": ""}],
  "personas": [{"name": "", "who": "", "pain": "", "hook": ""}],
  "insights": ["3–5 strings"],
  "sources": ["urls"],
  "confidence": "low|medium|high",
  "message_to_team": ""
}
```

### 6.3 Strategist — "Bram"
- **Mission:** positioning, channels, 2-week plan; in later rounds, revise based on Analyst.
- **Inputs:** brief, researcher output; (round ≥2) analyst report + recommendations.
- **Output:**
```json
{
  "positioning": "string, one sentence",
  "key_messages": ["3 strings"],
  "channels": [{"channel": "instagram|linkedin|x|landing", "role": "", "cadence": ""}],
  "two_week_plan": [{"week": 1, "focus": "", "posts": 0}, {"week": 2, "focus": "", "posts": 0}],
  "success_metric": "string",
  "changes_from_previous_round": "string or null",
  "message_to_team": ""
}
```

### 6.4 Copywriter — "Lena"
- **Mission:** produce the posts; self-flag risks.
- **Inputs:** brief, strategist output, `num_posts`; (revision) veto notes, analyst recommendations.
- **Output:**
```json
{
  "items": [
    {
      "channel": "instagram|linkedin|x",
      "headline": "≤60 chars",
      "body": "channel-appropriate length",
      "cta": "",
      "hashtags": ["..."],
      "rationale": "why this post, one sentence",
      "risk_flags": ["unverified claim", "competitor mention", "price promise", ...] 
    }
  ],
  "message_to_team": ""
}
```
- **Guidance:** one item per channel in the plan; vary formats (hook post, story post, offer post). Never invent statistics; if a number appears, flag it. Round ≥2: at least one item must explicitly implement an Analyst recommendation and say so in `rationale`.

### 6.5 Designer — "Kofi"
- **Mission:** one visual per post, expressed as a **poster spec** the code renders to SVG.
- **Inputs:** content items (headline, body, channel), brief tone.
- **Output:**
```json
{
  "assets": [
    {
      "content_index": 0,
      "headline": "≤6 words, may differ from post headline",
      "subline": "≤12 words",
      "palette": {"bg": "#hex", "fg": "#hex", "accent": "#hex"},
      "layout": "stacked|split|badge",
      "glyph": "single emoji or short symbol",
      "alt_text": ""
    }
  ],
  "message_to_team": ""
}
```
- **Renderer (`render_poster_svg`)**: 1080×1080 viewBox; three layout templates; big type; palette from spec; glyph large in a corner; product name small at bottom. Sanitize hex values. This is ~60 lines and never fails. Keep the fonts to a system stack so SVGs render identically everywhere.

### 6.6 Publisher — "Tariq"
- **Mission:** ship approved content; write the campaign page intro.
- **Inputs:** approved content list, brief.
- **Output:**
```json
{
  "campaign_headline": "string for the campaign page",
  "campaign_intro": "≤60 words",
  "schedule": [{"content_index": 0, "publish_slot": "Day 1 09:00", "channel": ""}],
  "message_to_team": ""
}
```
- The runtime does the actual publishing (status change). The LLM call is small; its value is the page copy and a plausible schedule.

### 6.7 Analyst — "Mei"
- **Mission:** read metrics, explain, recommend.
- **Inputs:** metrics per content item (aggregated), the content itself, strategy.
- **Output:**
```json
{
  "report": "≤120 words, plain language, for the board",
  "winner_content_id": "string",
  "loser_content_id": "string",
  "findings": ["3 strings, each tying a number to a reason"],
  "recommendations": [{"action": "", "why": "", "target_agent": "strategist|copywriter"}],
  "message_to_team": ""
}
```
- **Trigger:** `POST /api/briefs/{id}/simulate_day` → `simulate_metrics()` writes a day of metrics → creates an **analyst** task. Not on a timer; you control the moment in the demo.

### 6.8 CFO — "Otto"
- **Mission:** guard the budget, report spend.
- **Inputs:** spend by agent, budget, remaining planned tasks.
- **Output:**
```json
{
  "memo": "≤80 words",
  "burn_assessment": "ok|warning|critical",
  "action": "continue|pause|reduce_scope",
  "message_to_team": ""
}
```
- Triggered by the budget guard (5.4) and at campaign end (after Publisher, round 1) for a closing memo.

---

## 7. Tools (`tools.py`)

| function | behaviour | mock |
|---|---|---|
| `web_search(query, k=5)` | Tavily `POST /search`, return `[{title,url,snippet}]` | returns 3 canned results from `mocks/search.json` |
| `render_poster_svg(spec)` | pure function → SVG string | n/a (always real) |
| `publish(content_id)` | set status/published_at; return campaign URL | same |
| `simulate_metrics(brief_id, day)` | for each published content: base impressions by channel (x: 1200±, linkedin: 800±, instagram: 1500±), CTR 1–4%, likes 2–6% of impressions; **bias**: the content whose headline is shortest (or contains a number) gets ×1.8 engagement so a clear winner emists; one item gets ×0.4 so there's a clear loser. Deterministic seed = brief_id + day | same |
| `call_llm(agent_key, system, user, schema)` | Anthropic messages API; returns (text, tokens_in, tokens_out) | returns `mocks/{agent_key}.json` with a 1.5s sleep for realism |

**Stretch (only if everything else works):** `post_to_x()` via X API, or `post_to_buffer()`. Do not start this before 2:30.

---

## 8. API surface (FastAPI)

| method & path | purpose |
|---|---|
| `GET /` | serves dashboard.html |
| `GET /api/state` | **single polling endpoint**: company, agents (with status), current brief, tasks, last 50 messages, content (with asset svg), pending approvals, spend summary, metrics summary. One call, one JSON, keep the UI dumb |
| `POST /api/briefs` | create brief → create CEO task (`ready`) → returns brief id |
| `POST /api/briefs/demo` | inserts the prepared demo brief (one-click for the demo) |
| `POST /api/approvals/{content_id}` body `{decision, note}` | approve/veto; writes board message; on veto with note → creates a Copywriter revision task for that single item (optional; can just drop the item) |
| `POST /api/briefs/{id}/simulate_day` | writes metrics for next day, creates Analyst task |
| `POST /api/company/pause` / `resume` | kill switch; sets all agents `paused`/`idle`; bus alert |
| `POST /api/tasks/{id}/retry` | reset failed task |
| `POST /api/reset` | wipe all tables except agents & company defaults (**demo re-run button**; protect with a confirm in UI) |
| `GET /campaign/{brief_id}` | public campaign page (Jinja2) |
| `GET /api/content/{id}/asset.svg` | serves the SVG with `image/svg+xml` |

---

## 9. Board dashboard (`static/dashboard.html`)

Purpose: make the invisible company visible. Everything on one screen, no navigation.

**Layout (desktop, 3 columns):**
```
┌──────────────────────────────────────────────────────────────────────┐
│ Ghost Agency   ● running   spent €0.42 / €5.00 ▓▓░░░░   [Pause] [Reset]│
├───────────────┬─────────────────────────────┬────────────────────────┤
│ Org chart     │ Team channel (bus feed)     │ Board inbox            │
│ 8 agent cards │ newest at bottom, autoscroll│ approvals: card per    │
│ name·role     │ "Nora → Bram: Market is …"  │ post: channel, headline│
│ status dot    │ kinds colored (alert red,   │ body, poster thumbnail │
│ current task  │ report green, handoff blue) │ risk flags, rationale  │
│ tokens/cost   │                             │ [Approve] [Veto+note]  │
├───────────────┴─────────────────────────────┴────────────────────────┤
│ Campaign: tasks pipeline (round 1 · round 2) ── content grid ── metrics│
│ [Paste demo brief] [Simulate day →]  link: /campaign/{id}             │
└──────────────────────────────────────────────────────────────────────┘
```

**Behaviour**
- Poll `/api/state` every 2s; diff-render the bus feed (append only) so autoscroll doesn't jump.
- Agent status dot: grey idle, pulsing amber working, red blocked, striped paused. The pulse is the *only* animation on the page.
- Approve/Veto are immediate; veto opens a one-line note input.
- Empty states are instructions: "No brief yet. Paste the demo brief to start the company."
- Spend meter turns amber at 80%, red at 100%.

**Visual direction (so it doesn't look like every hackathon dashboard):** this is a control room for a company that doesn't exist. Go for something specific: a monochrome ink-on-paper feel (off-white background, near-black type, one signal colour used **only** for agent activity and alerts), one typeface (e.g. a geometric grotesk or a humanist sans), dense but calm. No gradients, no card shadows, no all-caps labels. Visible focus states. Fits in 300 lines of CSS.

---

## 10. Public campaign page (`/campaign/{id}`)

- Header: product name, `campaign_headline`, `campaign_intro` from Publisher.
- Grid of published posts: poster SVG, channel badge, headline, body, CTA, hashtags, "published Day N".
- After a simulated day: small metrics line under each post (impressions · CTR · likes).
- Round-2 posts get a subtle "revised after day 1" tag — visible proof of the loop.
- Footer: "Produced by Ghost Agency. No humans were employed in the making of this campaign." (Judges will quote it.)

---

## 11. End-to-end flows

### 11.1 Round 1 — from brief to live campaign
1. Board pastes brief → `briefs` row + CEO task `ready`.
2. Tick → CEO runs → 4 tasks created; Researcher `ready`, others `blocked`.
3. Researcher: runtime searches → LLM → memo; message "Bram, research is in: …".
4. Strategist `ready` → runs → plan.
5. Copywriter → 3–4 items → `content(pending_approval)` + `approvals` + Designer task.
6. Designer → SVG posters attached. Board inbox now shows posts with visuals.
7. Board approves ≥1 (vetoes one for drama; the note "too salesy" flows back if you implement single-item revision).
8. Publisher gate opens → Publisher runs → content `published`; brief `live`; bus message with URL.
9. CFO closing memo (round 1).

Expected wall time in real mode: 60–120s. In mock mode: ~15s.

### 11.2 Round 2 — the loop closes
1. Board clicks **Simulate day** → metrics day 1 → Analyst task.
2. Analyst: report + winner/loser + recommendations → bus (kind `report`).
3. Runtime creates Strategist (revision) → Copywriter → Designer → Publisher chain, `round=2`.
4. Strategist output has `changes_from_previous_round`; Copywriter's new items cite the recommendation.
5. Board approves → Publisher ships round 2 → campaign page shows revised posts.

Optional round 3 to show it keeps going; not needed for the pitch.

### 11.3 Governance flows
- **Veto**: item `vetoed`; Publisher ignores it; board message on bus.
- **Kill switch**: `company.paused=1`; orchestrator skips; running agent finishes its current call; all agents shown paused; bus alert "Board paused the company."
- **Budget breach**: guard pauses + CFO memo explains.

---

## 12. Repository layout (for Claude Code)

```
ghost-agency/
  README.md               # run instructions + demo script
  DESIGN.md               # this document
  .env.example
  requirements.txt
  main.py                 # FastAPI app, routes, startup (init db, seed agents, start orchestrator)
  db.py                   # connection, schema DDL, helpers (get/insert/update, json columns)
  models.py               # Pydantic output models per agent (validation)
  agents.py               # AGENTS = {key: {display_name, role_summary, system_prompt, output_schema}}
  runtime.py              # run_agent, orchestrator loop, hooks, budget guard
  tools.py                # call_llm, web_search, render_poster_svg, simulate_metrics, publish
  demo_brief.json         # the prepared brief
  mocks/                  # ceo.json researcher.json … search.json
  static/dashboard.html   # single-file UI
  templates/campaign.html # Jinja2
  tests/test_smoke.py     # runs whole round 1 + round 2 in MOCK mode, asserts states
```

**Suggested build order in Claude Code (each step runnable):**
1. `db.py` + `agents.py` seed + `main.py` with `/api/state` returning empty structures. Run server.
2. `tools.call_llm` with mock mode + `runtime.run_agent` + orchestrator; CEO → Researcher works in mock mode.
3. Remaining hooks; round 1 completes in mock mode; `tests/test_smoke.py` passes.
4. `dashboard.html` against `/api/state`.
5. Approvals + Publisher gate + campaign page.
6. Metrics simulation + Analyst + round 2 chain. Smoke test extended.
7. Switch `MOCK_LLM=0`, tune prompts on real output. Fix JSON issues.
8. Design pass on dashboard and campaign page. Record backup video.

---

## 13. Demo brief (prepare tonight)

```json
{
  "product_name": "Nachtfiets",
  "one_liner": "A €9/month bike-light subscription that replaces your lights automatically when they get stolen.",
  "description": "Amsterdam cyclists lose bike lights constantly. Nachtfiets ships a pair of bright, tool-free clip lights and replaces them free within 24h whenever they are stolen or broken. Cancel anytime.",
  "audience": "Amsterdam commuters and students aged 18–35 who cycle daily and have had lights stolen at least once.",
  "goals": "300 waitlist signups in 2 weeks before the October launch.",
  "budget_eur": 500,
  "tone": "playful, Dutch-direct, a little cheeky, never corporate",
  "channels": ["instagram", "linkedin", "x"]
}
```
Have a second brief ready (something totally different, e.g. a B2B invoicing tool) in case a judge says "does it only do bikes?".

---

## 14. Three-hour build plan (team of 3)

| time | A — runtime | B — dashboard | C — pages, tools, content |
|---|---|---|---|
| 0:00–0:15 | all: agree schema (§4), agent prompts pasted into `agents.py`, repo + env | | |
| 0:15–1:15 | db, run_agent, orchestrator, CEO/Researcher/Strategist/Copywriter hooks, mock mode | dashboard skeleton against `/api/state`, org chart + bus feed | `render_poster_svg`, `simulate_metrics`, mocks/*.json, campaign template |
| 1:15–1:45 | Designer/Publisher hooks, approvals gate, endpoints | approvals inbox, spend meter, pause/reset buttons | campaign page wired, asset endpoint |
| 1:45–2:15 | Analyst + round-2 chain, CFO guard | content grid + metrics line, round tags | real web search, prompt tuning with real LLM |
| 2:15–2:40 | full real-mode run, fix failures | polish, empty states, focus states | second brief, README demo script |
| 2:40–3:00 | all: record backup video in mock mode + real mode; freeze code | | |

Rules: `main` always runs. Merge every 20 minutes. Nobody starts a stretch goal before 2:30.

---

## 15. Demo script (5 minutes)

1. **(0:00)** Dashboard on screen, empty. "There are 42 people in this room. Our company has zero employees. These eight are the whole staff." Point at the idle org chart.
2. **(0:30)** Click *Paste demo brief*. Iris (CEO) lights up, tasks appear, Nora starts researching. Read one bus message aloud.
3. **(1:30)** Posts arrive in the Board inbox with posters. "This is the only moment a human is needed." Approve two, veto one with a note.
4. **(2:15)** Tariq publishes. Open `/campaign/{id}` in a second tab. Show the footer.
5. **(2:45)** Click *Simulate day*. Mei's report appears; Bram revises; Lena writes new posts citing the report. "Nobody asked them to. That's the company running itself."
6. **(3:45)** Spend meter: "€0.60 for a campaign an agency bills €3,000 for. Otto the CFO would pause the company if we crossed budget." Hit Pause once to show the kill switch, resume.
7. **(4:15)** Business slide: €149/month, ~€5 marginal cost, scales by spawning agents not hiring. Close: "No jurisdiction lets a company have zero humans, and autonomous agents make bad calls — so we built the board first. Governance is the product."

Have the backup video ready in a third tab.

---

## 16. Business proposal (one page, for the slide)

- **Problem:** 1.2M+ small businesses in the Netherlands alone; most do marketing badly or not at all because agencies cost €3–8k/month and DIY tools still require a person to run them.
- **Product:** a continuously running marketing department as a subscription. Input: a brief. Output: live campaigns that improve weekly.
- **Pricing:** €149/month per product (research, ~20 posts/month, weekly optimization, board dashboard). €49 one-off campaign kit as entry.
- **Unit economics:** ~€3–8/month in LLM + search + image costs per client → >90% gross margin. Scales linearly with compute, not headcount.
- **Go-to-market:** start with Amsterdam founders/hackathon communities (they're in the room); Product Hunt launch produced *by the agency itself* (meta, memorable).
- **Moat:** the governance layer (approvals, spend guards, audit trail on the bus) — the thing clients, payment providers and regulators will demand from any unstaffed company. Competitors sell content generation; we sell an accountable workforce.
- **Risks (say them first):** legal status of autonomous companies, brand-safety failures, platform API access for posting, model cost volatility. Mitigations: human board by design, risk flags on every post, multi-channel abstraction, per-client budget guard.
- **Roadmap:** real channel publishing → paid ads agent with real spend → sales agent (outbound) → a second "department" (support) → Ghost Company: the same runtime running any service business.

---

## 17. Open decisions for the team (settle tonight)
1. SQLite vs. Supabase (recommendation: SQLite).
2. Vanilla HTML dashboard vs. Streamlit (recommendation: HTML; Streamlit if nobody is comfortable with JS).
3. Single-item revision on veto (nice) vs. simply dropping vetoed items (simpler). Recommendation: drop for v1, add revision if time remains.
4. Names for the agents — keep them; they make the org chart feel like a company.
5. Who owns the backup video and the pitch (one person, not the person debugging at 2:50).
