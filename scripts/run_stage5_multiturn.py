#!/usr/bin/env python3
"""可复现的多轮用例：冰箱主路径追问运费与无理由，复用已查订单事实。"""

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
from app.dialogue import DialogueSession, answer_turn  # noqa: E402
from app.orchestrator import FRIDGE_QUESTION, check_fridge_answer  # noqa: E402
from scripts.seed_stage4 import MEMBER_EXTERNAL_ID, seed  # noqa: E402

TURNS = [
    {"id": "T1", "question": FRIDGE_QUESTION, "keyword": "冰箱"},
    {"id": "T2", "question": "那运费呢？"},
    {"id": "T3", "question": "那会员能延长七天无理由吗？"},
]


def compact_turn(result: dict) -> dict:
    return {
        "ok": result.get("ok"),
        "question": result.get("question"),
        "answer": result.get("answer"),
        "error_type": result.get("error_type"),
        "order_reused": result.get("order_reused"),
        "order_fields_injected": result.get("order_fields_injected"),
        "citations": result.get("citations"),
        "model": result.get("model"),
        "usage": result.get("usage"),
        "n_tool_calls": len(result.get("tool_calls") or []),
        "tool_calls_brief": [
            {
                "tool": c.get("tool"),
                "reused": c.get("reused"),
                "arguments": c.get("arguments"),
            }
            for c in (result.get("tool_calls") or [])
        ],
        "conflict_explanation": (result.get("conflict_resolution") or {}).get("explanation"),
        "review": result.get("review"),
        "meets_expected": result.get("meets_expected"),
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    with psycopg.connect(settings.dsn()) as conn:
        seed(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, external_id, display_name FROM users WHERE external_id = %s",
                (MEMBER_EXTERNAL_ID,),
            )
            row = cur.fetchone()
    if not row:
        print(json.dumps({"ok": False, "error_type": "seed_user_missing"}, ensure_ascii=False, indent=2))
        return 1
    ctx = SessionContext(user_id=int(row[0]), external_id=row[1], display_name=row[2])
    session = DialogueSession(ctx=ctx)
    turns_out = []
    ok = True
    for spec in TURNS:
        result = answer_turn(session, spec["question"], keyword=spec.get("keyword"))
        item = compact_turn(result)
        item["id"] = spec["id"]
        turns_out.append(item)
        if not result.get("ok"):
            ok = False
            break

    t1, t2, t3 = (turns_out + [None, None, None])[:3]
    freight_ok = False
    if t2 and t2.get("ok"):
        text = t2.get("answer") or ""
        freight_ok = ("运费" in text) and ("商家" in text or "卖家" in text)
    member_ok = False
    if t3 and t3.get("ok"):
        text = t3.get("answer") or ""
        member_ok = ("会员" in text) and any(s in text for s in ("不延长", "不能延长", "不会延长", "不改变"))

    reused_ok = bool(t2 and t2.get("order_reused") and t3 and t3.get("order_reused"))
    same_order = False
    if t1 and t2 and t3 and t1.get("ok") and t2.get("ok") and t3.get("ok"):
        nos = [
            (t.get("order_fields_injected") or {}).get("order_no")
            for t in (t1, t2, t3)
        ]
        same_order = len(set(nos)) == 1 and nos[0] == "SO-FRIDGE-20D"

    t1_review = None
    if t1 and t1.get("ok"):
        t1_review = check_fridge_answer(t1.get("answer") or "", t1.get("order_fields_injected") or {})

    report = {
        "ok": bool(ok and reused_ok and same_order and (t1_review or {}).get("meets_expected") and freight_ok and member_ok),
        "name": "stage5_multiturn_fridge",
        "model": "qwen3.8-flash",
        "session_id": session.session_id,
        "turns": turns_out,
        "checks": {
            "t1_fridge_path": t1_review,
            "t2_reused_order": bool(t2 and t2.get("order_reused")),
            "t3_reused_order": bool(t3 and t3.get("order_reused")),
            "same_order_so_fridge_20d": same_order,
            "t2_freight_merchant": freight_ok,
            "t3_member_does_not_extend": member_ok,
        },
        "note": "第一轮查冰箱订单，后两轮追问运费与会员，必须复用 SO-FRIDGE-20D 的已查事实，不得丢上下文。",
    }
    out = ROOT / "reports" / "stage5_multiturn.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": report["ok"],
        "report": str(out),
        "checks": report["checks"],
        "answers": [{"id": t["id"], "question": t["question"], "order_reused": t.get("order_reused"), "answer": t.get("answer"), "error_type": t.get("error_type")} for t in turns_out],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
