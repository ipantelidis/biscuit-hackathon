import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "MOCK_LLM": "1", "MOCK_SEARCH": "1", "MOCK_LLM_DELAY": "0", "MOTION_RENDER": "0",
    "ORCHESTRATOR_AUTOSTART": "0", "BUDGET_EUR": "5.0", "PACE_SECONDS": "0", "NO_PHOTOS": "1", "VIDEO_GEN": "0",
    "GHOST_DB_PATH": str(ROOT / "tests" / "_smoke.db"),
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402


@pytest.fixture()
def client():
    for suffix in ("", "-wal", "-shm"):
        p = Path(os.environ["GHOST_DB_PATH"] + suffix)
        if p.exists():
            p.unlink()
    db.close()
    import main
    with TestClient(main.app) as c:
        yield c
    db.close()
