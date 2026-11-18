#!/usr/bin/env python3
"""跑通冰箱主路径并写入 reports/stage4_fridge_path.json。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.context import SessionContext  # noqa: E402
from app.orchestrator import FRIDGE_QUESTION, answer_after_sales  # noqa: E402
from app.tools import tool_signatures  # noqa: E402
from scripts.seed_stage4 import MEMBER_EXTERNAL_ID, seed  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    with psycopg.connect(settings.dsn()) as conn:
        seed_report = seed(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, external_id, display_name FROM users WHERE external_id = %s", (MEMBER_EXTERNAL_ID,))
            row = cur.fetchone()
    if not row:
        report = {"ok": False, "error_type": "seed_user_missing"}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    ctx = SessionContext(user_id=int(row[0]), external_id=row[1], display_name=row[2])
    result = answer_after_sales(FRIDGE_QUESTION, ctx, keyword="冰箱")
    result["tool_signatures"] = tool_signatures()
    result["seed"] = {
        "fridge_order_no": next(
            o["order_no"] for o in seed_report["orders"] if o.get("purpose") == "fridge_main_path"
        )
    }
    out = ROOT / "reports" / "stage4_fridge_path.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 控制台只打印摘要，避免把整份证据刷屏；完整结果在报告文件里。
    summary = {
        "ok": result.get("ok"),
        "report": str(out),
        "question": result.get("question"),
        "order_fields_injected": result.get("order_fields_injected"),
        "n_tool_calls": len(result.get("tool_calls") or []),
        "model": result.get("model"),
        "meets_expected": result.get("meets_expected"),
        "error_type": result.get("error_type"),
        "answer": result.get("answer"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") and result.get("meets_expected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
