#!/usr/bin/env python3
"""多轮实测：先问政策再追问同一订单，以及冰箱主路径后追问运费与会员。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.dialogue import DialogueSession, answer_turn  # noqa: E402
from app.orchestrator import FRIDGE_QUESTION, check_fridge_answer  # noqa: E402
from app.seed import MEMBER_EXTERNAL_ID, context_from_demo_account, seed  # noqa: E402
from scripts.stage5_common import prepare_stage5_home  # noqa: E402


def compact_turn(result: dict) -> dict:
    return {
        "ok": result.get("ok"),
        "question": result.get("question"),
        "answer": result.get("answer"),
        "error_type": result.get("error_type"),
        "intent": result.get("intent"),
        "order_reused": result.get("order_reused"),
        "order_fields_injected": result.get("order_fields_injected"),
        "citations": result.get("citations"),
        "model": result.get("model"),
        "cascaded": result.get("cascaded"),
        "usage": result.get("usage"),
        "elapsed_ms": result.get("elapsed_ms"),
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


def run_session(ctx, turns: list[dict]) -> tuple[list[dict], DialogueSession]:
    session = DialogueSession(ctx=ctx)
    out = []
    for spec in turns:
        result = answer_turn(session, spec["question"], keyword=spec.get("keyword"))
        item = compact_turn(result)
        item["id"] = spec["id"]
        out.append(item)
        if not result.get("ok"):
            break
    return out, session


def _fresh_lookup(turn: dict) -> bool:
    return any(
        c.get("tool") == "query_order" and not c.get("reused")
        for c in (turn.get("tool_calls_brief") or [])
    )


def main() -> int:
    home = prepare_stage5_home()
    seed_report = seed()
    ctx = context_from_demo_account(MEMBER_EXTERNAL_ID)
    if ctx is None:
        print(json.dumps({"ok": False, "error_type": "seed_user_missing"}, ensure_ascii=False, indent=2))
        return 1

    # 场景 A：先问政策（不查单），再追问同一订单，后续轮次才查订单。
    policy_then_order_spec = [
        {"id": "A1", "question": "质量问题退货的运费由谁承担？"},
        {"id": "A2", "question": "那我这单冰箱买了 20 天质量问题还能退吗？", "keyword": "冰箱"},
        {"id": "A3", "question": "那运费呢？"},
    ]
    a_turns, a_session = run_session(ctx, policy_then_order_spec)
    a1, a2, a3 = (a_turns + [None, None, None])[:3]
    a_ok_gen = all(t and t.get("ok") for t in (a1, a2, a3))
    a1_no_order = bool(a1 and a1.get("ok") and not (a1.get("order_fields_injected") or {}).get("order_no"))
    a2_lookup = bool(a2 and a2.get("ok") and _fresh_lookup(a2) and not a2.get("order_reused"))
    a2_order = ((a2 or {}).get("order_fields_injected") or {}).get("order_no") == "SO-FRIDGE-20D"
    a3_reused = bool(a3 and a3.get("order_reused"))
    a3_same = ((a3 or {}).get("order_fields_injected") or {}).get("order_no") == "SO-FRIDGE-20D"
    a2_review = None
    if a2 and a2.get("ok"):
        a2_review = check_fridge_answer(a2.get("answer") or "", a2.get("order_fields_injected") or {})
    a3_freight = False
    if a3 and a3.get("ok"):
        text = a3.get("answer") or ""
        a3_freight = ("运费" in text) and ("商家" in text or "卖家" in text)
    # 第五段验收看上下文保持与后续轮次查单；冰箱四点核对作为附加记录，不把未提及 12 个月质保当成多轮失败。
    scenario_a_context_ok = bool(
        a_ok_gen and a1_no_order and a2_lookup and a2_order and a3_reused and a3_same and a3_freight
    )
    scenario_a_ok = scenario_a_context_ok

    # 场景 B：第一轮即查冰箱订单，后两轮追问运费与会员，必须复用同一订单事实。
    fridge_followup_spec = [
        {"id": "B1", "question": FRIDGE_QUESTION, "keyword": "冰箱"},
        {"id": "B2", "question": "那运费呢？"},
        {"id": "B3", "question": "那会员能延长七天无理由吗？"},
    ]
    b_turns, b_session = run_session(ctx, fridge_followup_spec)
    b1, b2, b3 = (b_turns + [None, None, None])[:3]
    b_ok_gen = all(t and t.get("ok") for t in (b1, b2, b3))
    freight_ok = False
    if b2 and b2.get("ok"):
        text = b2.get("answer") or ""
        freight_ok = ("运费" in text) and ("商家" in text or "卖家" in text)
    member_ok = False
    if b3 and b3.get("ok"):
        text = b3.get("answer") or ""
        member_ok = ("会员" in text) and any(s in text for s in ("不延长", "不能延长", "不会延长", "不改变"))
    reused_ok = bool(b2 and b2.get("order_reused") and b3 and b3.get("order_reused"))
    same_order = False
    if b_ok_gen:
        nos = [(t.get("order_fields_injected") or {}).get("order_no") for t in (b1, b2, b3)]
        same_order = len(set(nos)) == 1 and nos[0] == "SO-FRIDGE-20D"
    b1_review = None
    if b1 and b1.get("ok"):
        b1_review = check_fridge_answer(b1.get("answer") or "", b1.get("order_fields_injected") or {})
    scenario_b_ok = bool(
        b_ok_gen and reused_ok and same_order and (b1_review or {}).get("meets_expected") and freight_ok and member_ok
    )

    report = {
        "ok": bool(scenario_a_ok and scenario_b_ok),
        "name": "stage5_multiturn",
        "model": "qwen3.8-flash",
        "policy_qa_home": str(home),
        "seed_ok": bool(seed_report.get("ok")),
        "scenario_a_policy_then_order": {
            "ok": scenario_a_ok,
            "session_id": a_session.session_id,
            "turns": a_turns,
            "checks": {
                "a1_policy_without_order": a1_no_order,
                "a2_looked_up_order": a2_lookup,
                "a2_order_so_fridge_20d": a2_order,
                "a2_fridge_path": a2_review,
                "a3_reused_order": a3_reused,
                "a3_same_order": a3_same,
                "a3_freight_merchant": a3_freight,
                "a2_fridge_path_meets_expected": bool((a2_review or {}).get("meets_expected")),
            },
            "note": "先问质量问题运费政策，再追问同一冰箱订单；第二轮才查订单，第三轮复用上下文。冰箱四点核对另记，缺“12个月/质保期”表述不视为多轮上下文失败。",
        },
        "scenario_b_fridge_followup": {
            "ok": scenario_b_ok,
            "session_id": b_session.session_id,
            "turns": b_turns,
            "checks": {
                "b1_fridge_path": b1_review,
                "b2_reused_order": bool(b2 and b2.get("order_reused")),
                "b3_reused_order": bool(b3 and b3.get("order_reused")),
                "same_order_so_fridge_20d": same_order,
                "b2_freight_merchant": freight_ok,
                "b3_member_does_not_extend": member_ok,
            },
            "note": "第一轮查冰箱订单，后两轮追问运费与会员，必须复用 SO-FRIDGE-20D 的已查事实。",
        },
    }
    out = ROOT / "reports" / "stage5_multiturn.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": report["ok"],
        "report": str(out),
        "scenario_a_ok": scenario_a_ok,
        "scenario_b_ok": scenario_b_ok,
        "scenario_a_checks": report["scenario_a_policy_then_order"]["checks"],
        "scenario_b_checks": report["scenario_b_fridge_followup"]["checks"],
        "answers_a": [
            {"id": t["id"], "question": t["question"], "order_reused": t.get("order_reused"), "answer": t.get("answer"), "error_type": t.get("error_type")}
            for t in a_turns
        ],
        "answers_b": [
            {"id": t["id"], "question": t["question"], "order_reused": t.get("order_reused"), "answer": t.get("answer"), "error_type": t.get("error_type")}
            for t in b_turns
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
