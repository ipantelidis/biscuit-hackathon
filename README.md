# Ghost Agency

A marketing agency with zero employees. Paste a product brief; twelve AI agents research,
strategise, write, review, design, publish, film, buy media, talk to the audience, measure and
iterate. The CEO hires specialists when a brief needs one. Humans sit only on the board.

See [DESIGN.md](DESIGN.md) for the full design. This file is the run book.

## Run

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                # MOCK_LLM=1 by default: runs with no keys, no network
.venv/bin/uvicorn main:app --reload # dashboard on http://localhost:8000
```

The campaign video is recorded locally with Chromium; run `.venv/bin/python -m playwright install chromium`
once (or set `MOTION_RENDER=0` to keep the animated HTML version only).

Real mode: set `ANTHROPIC_API_KEY` in `.env`, set `MOCK_LLM=0` (and `MOCK_SEARCH=0` with a
`TAVILY_API_KEY` if you want live research). The model is `LLM_MODEL` (default `claude-sonnet-5`);
`LLM_EFFORT=low` keeps it quick: a full run (round 1, a simulated day, round 2, all twelve agents plus a hire)
takes about 4.5 minutes and costs about EUR 0.40.

Tests (whole company, round 1 and round 2, in mock mode):

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

| file | what |
|---|---|
| `main.py` | FastAPI app: `/`, `/api/state`, actions, `/campaign/{id}` |
| `db.py` | SQLite schema and helpers; the shared brain |
| `agents.py` | the twelve agents, the task modes (answer, standup, board report, revise) and the specialist template |
| `models.py` | Pydantic validation of every agent output |
| `runtime.py` | `run_agent`, orchestrator tick, post-processing hooks, budget guard |
| `tools.py` | `call_llm` (real + mock), `web_search`, `render_poster_svg`, `simulate_metrics` |
| `mocks/` | canned outputs per agent (`*_r2.json` = revision round, `*_<mode>.json` = modes, `specialist.json` = any hire) |
| `media/` | rendered campaign videos (gitignored) |
| `static/dashboard.html` | the board dashboard, single file, polls `/api/state` every 2 s |
| `templates/campaign.html` | the public campaign page |
| `demo_brief.json`, `demo_brief_2.json` | Nachtfiets (bike lights) and Factuurtje (invoicing) |

## API

| method & path | purpose |
|---|---|
| `GET /api/state` | everything the dashboard shows, one JSON |
| `POST /api/briefs` | create a brief (JSON body) and the CEO task |
| `POST /api/briefs/demo?which=1` | one-click demo brief (`which=2` for the second) |
| `POST /api/approvals/{content_id}` | `{"decision": "approved"\|"vetoed", "note": ""}` |
| `POST /api/briefs/{id}/simulate_day` | one day of metrics, then the Analyst runs and round N+1 starts |
| `POST /api/company/pause` / `resume` | kill switch |
| `POST /api/company/pace` | `{"seconds": 6}` minimum seconds per agent task, for demo pacing |
| `GET /motion/{brief_id}` | the campaign video as a looping HTML animation |
| `GET /api/videos/{id}.webm` | the recorded video file |
| `POST /api/tasks/{id}/retry` | reset a failed task |
| `POST /api/reset` | wipe the campaign, keep agents and budget |
| `GET /campaign/{brief_id}` | public campaign page |
| `GET /api/content/{id}/asset.svg` | a poster |

## The company

| agent | job | when |
|---|---|---|
| Iris (CEO) | task graph, hires a specialist if the brief needs one, runs the daily standup, writes the board report | brief arrives; each simulated day; after each publish |
| Nora (Researcher) | market, competitors, personas; answers colleagues' questions | round 1 |
| Bram (Strategist) | positioning, channels, plan; revises on the analyst's findings; may ask Nora a question | every round |
| Lena (Copywriter) | posts with self-reported risk flags; rewrites what Sofia sends back | every round |
| Sofia (Compliance) | reviews every post before the board: clear, revise or block | every round |
| Kofi (Designer) | poster spec per post, rendered to SVG | every round |
| Tariq (Publisher) | ships approved posts, writes the campaign page | after the board decides |
| Jonas (Motion Designer) | storyboards the campaign video; the runtime renders and records it | round 1 |
| Jules (Paid Media) | splits the client's ad budget across channels; reallocates on measured cost per signup | after each publish |
| Pim (Community Manager) | replies to audience comments, reads the mood | each simulated day |
| Mei (Analyst) | reads metrics, ad spend and sentiment; recommends; starts the next round | each simulated day |
| Otto (CFO) | budget memos; the code-level guard pauses the company at 100 % | round 1 close, 80 % of budget |

Internal life: agents can ask each other questions on the bus (answered by a real call), every simulated
day opens with a standup, and vetoed posts feed back into the next round's brief.

## How the loop learns

- **Round memory.** The revision strategist depends on the latest research and its own previous plan, and
  gets a changelog of every change it made before. The copywriter sees every post the company ran, with
  body, rationale, days live, click-through per day and signups (last three rounds in full, older ones as
  headlines), and may not reuse a headline, opening line or hook family.
- **Analyst directives.** Each of Mei's recommendations carries a directive the runtime executes:
  `new_posts` (N posts), `drop_channel` (struck through on the dashboard; posts on it are discarded),
  `boost_channel` (one extra post there), `re_research` (Nora investigates a focus; the strategist waits for
  her), `hold` (no new posts this round). The runtime ranks posts by click-through per day and corrects
  Mei's winner and loser if her narrative contradicts the numbers.
- **Simulator with memory.** Attention decays after launch, hooks with a number, a short headline or a
  question earn more, a repeated opening line halves engagement, three posts of the same hook family
  saturate, and risk-flagged posts convert worse. Every metrics row stores its factors.
- **Veto routing.** A veto note goes to Lena, Kofi or both (chips on the veto form; the default is guessed
  from the note). A design-only veto sends the post back to Kofi for a new poster instead of killing it.
  Notes expire after one round for copy and two for design.
- **Posters that fit.** Headlines and sublines are shrunk to fit their column; nothing overflows.
- **Mock mode** supports three simulated days and says so on the fourth.

## Demo script (5 minutes)

1. **0:00** Dashboard empty. "There are 42 people in this room. Our company has zero employees. These eight are the whole staff."
2. **0:30** Click **Start the company** (or **Your own brief** and type one). Iris lights up, hires Femke the localiser, tasks appear, Nora researches. Read one bus message aloud. Bram asks Nora a question; she answers.
3. **1:30** Sofia reviews the posts and sends one back; Lena rewrites it. Posts arrive in the Board inbox with posters. "This is the only moment a human is needed." Approve two, veto one with a note ("too salesy").
4. **2:15** Tariq publishes. Jonas storyboards the video, Jules sets the ad plan, Iris files the board report. Open the campaign page: the video plays at the top. Show the footer.
5. **2:45** Click **Simulate day**. Iris runs the standup, comments arrive and Pim answers them, Mei's report lands, Bram revises, Lena writes new posts citing the report, Jules moves the ad money; approve; the page shows "revised after day 1". "Nobody asked them to."
6. **3:45** Spend meter. Otto's memo. Hit **Pause** to show the kill switch, resume.
7. **4:15** Business slide. Close: "Governance is the product."

Pace: the header has a Pace selector (fast / demo / slow) that sets the minimum time per agent task.

Backup: everything above runs in mock mode with no network. Before the demo: `POST /api/reset`
(the Reset button) and keep `MOCK_LLM=1` in a second checkout in case the API is slow.

## Governance in code

- Every post waits for a board decision; the Publisher gate opens only when all posts of a round are decided and at least one is approved.
- Budget guard: at 80 % the CFO is asked for a memo, at 100 % the company pauses itself.
- Every agent message and board action is on the bus, so the whole run is auditable.
