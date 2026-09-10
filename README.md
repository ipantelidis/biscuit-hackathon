# Ghost Agency

A marketing agency with zero employees. Paste a product brief; eight AI agents research,
strategise, write, design, publish, measure and iterate. Humans sit only on the board.

See [DESIGN.md](DESIGN.md) for the full design. This file is the run book.

## Run

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env                # MOCK_LLM=1 by default: runs with no keys, no network
.venv/bin/uvicorn main:app --reload # dashboard on http://localhost:8000
```

Real mode: set `ANTHROPIC_API_KEY` in `.env`, set `MOCK_LLM=0` (and `MOCK_SEARCH=0` with a
`TAVILY_API_KEY` if you want live research). The model is `LLM_MODEL` (default `claude-sonnet-5`);
`LLM_EFFORT=low` keeps a round under ~90 seconds.

Tests (whole company, round 1 and round 2, in mock mode):

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

| file | what |
|---|---|
| `main.py` | FastAPI app: `/`, `/api/state`, actions, `/campaign/{id}` |
| `db.py` | SQLite schema and helpers; the shared brain |
| `agents.py` | the eight agents: names, prompts, output schemas |
| `models.py` | Pydantic validation of every agent output |
| `runtime.py` | `run_agent`, orchestrator tick, post-processing hooks, budget guard |
| `tools.py` | `call_llm` (real + mock), `web_search`, `render_poster_svg`, `simulate_metrics` |
| `mocks/` | canned outputs per agent (`*_r2.json` = revision round) |
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
| `POST /api/tasks/{id}/retry` | reset a failed task |
| `POST /api/reset` | wipe the campaign, keep agents and budget |
| `GET /campaign/{brief_id}` | public campaign page |
| `GET /api/content/{id}/asset.svg` | a poster |

## Demo script (5 minutes)

1. **0:00** Dashboard empty. "There are 42 people in this room. Our company has zero employees. These eight are the whole staff."
2. **0:30** Click **Paste demo brief**. Iris lights up, tasks appear, Nora researches. Read one bus message aloud.
3. **1:30** Posts arrive in the Board inbox with posters. "This is the only moment a human is needed." Approve two, veto one with a note ("too salesy"). The note reaches Lena on the bus and in her next brief.
4. **2:15** Tariq publishes. Open the campaign page (link under the pipeline). Show the footer.
5. **2:45** Click **Simulate day**. Mei's report lands on the bus; Bram revises; Lena writes new posts citing the report; approve; the page shows "revised after day 1". "Nobody asked them to."
6. **3:45** Spend meter. Otto's memo. Hit **Pause** to show the kill switch, resume.
7. **4:15** Business slide. Close: "Governance is the product."

Backup: everything above runs in mock mode with no network. Before the demo: `POST /api/reset`
(the Reset button) and keep `MOCK_LLM=1` in a second checkout in case the API is slow.

## Governance in code

- Every post waits for a board decision; the Publisher gate opens only when all posts of a round are decided and at least one is approved.
- Budget guard: at 80 % the CFO is asked for a memo, at 100 % the company pauses itself.
- Every agent message and board action is on the bus, so the whole run is auditable.
