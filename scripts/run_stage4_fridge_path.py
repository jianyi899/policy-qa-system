#!/usr/bin/env python3
"""跑通冰箱主路径并写入 reports/stage4_fridge_path.json。数字与回答必须实测，不得编造。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import clear_settings_cache, get_settings  # noqa: E402
from app.orchestrator import FRIDGE_QUESTION, answer_after_sales  # noqa: E402
from app.seed import MEMBER_EXTERNAL_ID, context_from_demo_account, seed  # noqa: E402
from app.tools import invoke, tool_signatures  # noqa: E402
from app.seed import FRIDGE_ORDER_NO, FRIDGE_TRACKING  # noqa: E402
from app.tools.errors import ToolError  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    clear_settings_cache()
    settings = get_settings()
    seed_report = seed(settings)
    ctx = context_from_demo_account(MEMBER_EXTERNAL_ID, settings=settings)
    if ctx is None:
        report = {"ok": False, "error_type": "seed_user_missing"}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    logistics = None
    logistics_error = None
    try:
        logistics = invoke("query_logistics", {"order_no": FRIDGE_ORDER_NO}, ctx)
    except ToolError as exc:
        logistics_error = exc.error_type

    result = answer_after_sales(FRIDGE_QUESTION, ctx, keyword="冰箱")
    result["tool_signatures"] = tool_signatures()
    result["seed"] = {
        "fridge_order": next(
            o for o in seed_report["orders"] if o.get("purpose") == "fridge_main_path"
        ),
        "policy_qa_home": str(settings.home_dir()),
        "db_path": str(settings.db_path()),
    }
    result["logistics_check"] = {
        "ok": logistics is not None and logistics.get("ok"),
        "error_type": logistics_error,
        "result": logistics,
        "tracking_no": FRIDGE_TRACKING,
    }
    out = ROOT / "reports" / "stage4_fridge_path.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": result.get("ok"),
        "report": str(out),
        "question": result.get("question"),
        "order_fields_injected": result.get("order_fields_injected"),
        "n_tool_calls": len(result.get("tool_calls") or []),
        "query_order_called": any(
            c.get("tool") == "query_order" for c in (result.get("tool_calls") or [])
        ),
        "logistics_ok": result["logistics_check"]["ok"],
        "model": result.get("model"),
        "cascaded": result.get("cascaded"),
        "cascade": result.get("cascade"),
        "meets_expected": result.get("meets_expected"),
        "error_type": result.get("error_type"),
        "cited_policies": result.get("cited_policies"),
        "elapsed_ms": result.get("elapsed_ms"),
        "answer": result.get("answer"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") and result.get("meets_expected") and result["logistics_check"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
