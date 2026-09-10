"""SQLite layer: connection, schema, and small helpers.

The database is the shared brain: agents never talk to each other directly,
they read and write these tables. Everything is JSON-in-TEXT; no ORM.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

DB_PATH = os.environ.get("GHOST_DB_PATH", "ghost.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

JSON_COLUMNS = {
    "briefs": {"channels"},
    "tasks": {"input", "output", "depends_on"},
    "content": {"hashtags", "risk_flags"},
    "assets": {"spec"},
    "agents": {"output_schema"},
    "videos": {"spec"},
    "ad_plans": {"allocation"},
    "reports": {"body"},
}

# columns added after the first release; applied to existing databases on connect
MIGRATIONS = [
    ("content", "compliance_verdict", "TEXT"), ("content", "compliance_note", "TEXT"),
    ("metrics", "paid_impressions", "INTEGER DEFAULT 0"), ("metrics", "ad_spend_eur", "REAL DEFAULT 0"),
    ("agents", "hired", "INTEGER DEFAULT 0"), ("agents", "color", "TEXT"),
    ("company", "pace_seconds", "REAL DEFAULT 6"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
  id TEXT PRIMARY KEY, name TEXT, paused INTEGER DEFAULT 0,
  budget_eur REAL DEFAULT 5.0, spent_eur REAL DEFAULT 0.0, pace_seconds REAL DEFAULT 6,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS agents (
  key TEXT PRIMARY KEY, display_name TEXT, role_summary TEXT,
  system_prompt TEXT, output_schema TEXT,
  status TEXT DEFAULT 'idle', current_task_id TEXT,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_eur REAL DEFAULT 0.0,
  hired INTEGER DEFAULT 0, color TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS briefs (
  id TEXT PRIMARY KEY, product_name TEXT, one_liner TEXT, description TEXT,
  audience TEXT, goals TEXT, budget_eur REAL, tone TEXT, channels TEXT,
  status TEXT DEFAULT 'new', campaign_name TEXT, objective TEXT,
  campaign_headline TEXT, campaign_intro TEXT, day INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, brief_id TEXT, agent_key TEXT, title TEXT,
  input TEXT, output TEXT, depends_on TEXT, status TEXT DEFAULT 'blocked',
  attempt INTEGER DEFAULT 0, error TEXT, round INTEGER DEFAULT 1,
  started_at TEXT, finished_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, brief_id TEXT, from_agent TEXT, to_agent TEXT,
  kind TEXT, body TEXT, task_id TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS content (
  id TEXT PRIMARY KEY, brief_id TEXT, task_id TEXT, channel TEXT,
  headline TEXT, body TEXT, cta TEXT, hashtags TEXT, rationale TEXT,
  risk_flags TEXT, status TEXT DEFAULT 'draft', asset_id TEXT,
  published_at TEXT, publish_slot TEXT, round INTEGER DEFAULT 1,
  compliance_verdict TEXT, compliance_note TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, content_id TEXT, kind TEXT DEFAULT 'svg_poster',
  spec TEXT, svg TEXT, alt_text TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  id TEXT PRIMARY KEY, content_id TEXT, brief_id TEXT, day INTEGER,
  impressions INTEGER, clicks INTEGER, likes INTEGER, shares INTEGER,
  signups INTEGER, ctr REAL, paid_impressions INTEGER DEFAULT 0, ad_spend_eur REAL DEFAULT 0,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, brief_id TEXT, task_id TEXT, title TEXT, spec TEXT, caption TEXT,
  status TEXT DEFAULT 'storyboard', path TEXT, duration_s REAL, error TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, brief_id TEXT, content_id TEXT, day INTEGER, label TEXT,
  author TEXT, channel TEXT, text TEXT, mood TEXT, reply TEXT, replied_at TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ad_plans (
  id TEXT PRIMARY KEY, brief_id TEXT, task_id TEXT, round INTEGER, allocation TEXT,
  expected_cpa_eur REAL, rationale TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY, brief_id TEXT, task_id TEXT, round INTEGER, day INTEGER,
  headline TEXT, body TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, content_id TEXT, requested_by TEXT,
  decision TEXT, decided_by TEXT, note TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS spend (
  id TEXT PRIMARY KEY, agent_key TEXT, task_id TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost_eur REAL, created_at TEXT, updated_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                cols = {r[1] for r in _conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def tx():
    """Serialise all DB access across the request threads and the orchestrator."""
    conn = connect()
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _encode(table: str, row: dict[str, Any]) -> dict[str, Any]:
    cols = JSON_COLUMNS.get(table, set())
    out = {}
    for k, v in row.items():
        if k in cols and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        out[k] = v
    return out


def _decode(table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_COLUMNS.get(table, set()):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def insert(table: str, row: dict[str, Any]) -> str:
    row = dict(row)
    if table != "agents":
        row.setdefault("id", new_id())
    ts = now()
    row.setdefault("created_at", ts)
    row.setdefault("updated_at", ts)
    row = _encode(table, row)
    keys = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with tx() as conn:
        conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks})", list(row.values()))
    return row.get("id", row.get("key"))


def update(table: str, id_value: str, fields: dict[str, Any], id_col: str = "id") -> None:
    fields = dict(fields)
    fields["updated_at"] = now()
    fields = _encode(table, fields)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE {id_col} = ?", [*fields.values(), id_value])


def get(table: str, id_value: str, id_col: str = "id") -> dict[str, Any] | None:
    conn = connect()
    with _lock:
        row = conn.execute(f"SELECT * FROM {table} WHERE {id_col} = ?", [id_value]).fetchone()
    return _decode(table, row)


def query(table: str, where: str = "", params: Iterable[Any] = (), order: str = "created_at ASC",
          limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = connect()
    with _lock:
        rows = conn.execute(sql, list(params)).fetchall()
    return [_decode(table, r) for r in rows]


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with tx() as conn:
        conn.execute(sql, list(params))


def scalar(sql: str, params: Iterable[Any] = ()) -> Any:
    conn = connect()
    with _lock:
        row = conn.execute(sql, list(params)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- seeding / reset

def seed_company(name: str, budget_eur: float) -> None:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT id FROM company LIMIT 1").fetchone()
    if row is None:
        insert("company", {"id": "company", "name": name, "paused": 0,
                           "budget_eur": budget_eur, "spent_eur": 0.0,
                           "pace_seconds": float(os.environ.get("PACE_SECONDS", "6"))})
    else:
        update("company", "company", {"budget_eur": budget_eur})


def seed_agents(agents: dict[str, dict[str, Any]]) -> None:
    """Upsert prompts/schemas every start so prompt edits take effect; keep counters."""
    for key, spec in agents.items():
        existing = get("agents", key, id_col="key")
        fields = {
            "display_name": spec["display_name"],
            "role_summary": spec["role_summary"],
            "system_prompt": spec["system_prompt"],
            "output_schema": spec["output_schema"],
        }
        if existing:
            update("agents", key, fields, id_col="key")
        else:
            insert("agents", {"key": key, "status": "idle", **fields})


def reset_all() -> None:
    """Wipe campaign data; keep agents and company defaults."""
    with tx() as conn:
        for t in ("briefs", "tasks", "messages", "content", "assets", "metrics", "approvals", "spend",
                  "videos", "comments", "ad_plans", "reports"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM agents WHERE hired = 1")
        conn.execute("UPDATE agents SET status='idle', current_task_id=NULL, tokens_in=0, tokens_out=0, cost_eur=0, updated_at=?", [now()])
        conn.execute("UPDATE company SET paused=0, spent_eur=0, updated_at=?", [now()])
