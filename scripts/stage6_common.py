"""第六段脚本共用逻辑。"""

from __future__ import annotations

import json
from pathlib import Path

from app.context import SessionContext
from app.db import connection
from scripts.seed_stage4 import MEMBER_EXTERNAL_ID

ROOT = Path(__file__).resolve().parents[1]


def load_questions(split: str | None = None) -> list[dict]:
    items = []
    for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if split is None or item.get("split") == split:
            items.append(item)
    return items


def load_member_context() -> SessionContext:
    with connection(read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, external_id, display_name FROM users WHERE external_id = %s",
                (MEMBER_EXTERNAL_ID,),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError("missing_stage4_member")
    return SessionContext(user_id=int(row[0]), external_id=row[1], display_name=row[2])


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_stage5_dev() -> dict:
    path = ROOT / "reports" / "stage5_dev_citation.json"
    return json.loads(path.read_text(encoding="utf-8"))
