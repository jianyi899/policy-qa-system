#!/usr/bin/env python3
"""开发集：实测句级引用精度，并单独核对 Q041（专类依据）。可用开发集观察提示，不跑测试集调参。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answer_check import check_q041_answer  # noqa: E402
from app.dialogue import answer_question  # noqa: E402
from scripts.stage5_common import prepare_stage5_home  # noqa: E402
from scripts.stage5_eval import load_questions, run_generation_split  # noqa: E402


def run_q041_first() -> dict:
    q = next(x for x in load_questions("dev") if x["id"] == "Q041")
    result = answer_question(q["question"], gold_spans=q.get("relevant_spans") or [])
    review = check_q041_answer(result.get("answer") or "")
    report = {
        "ok": bool(result.get("ok") and review.get("meets_expected")),
        "question_id": "Q041",
        "question": q["question"],
        "answer_points": q["answer_points"],
        "model": result.get("model"),
        "cascaded": result.get("cascaded"),
        "cascade": result.get("cascade"),
        "answer": result.get("answer"),
        "citations": result.get("citations"),
        "citation_score": result.get("citation_score"),
        "conflict_explanation": (result.get("conflict_resolution") or {}).get("explanation"),
        "by_scope_doc_ids": (result.get("conflict_resolution") or {}).get("by_scope_doc_ids"),
        "review": review,
        "error_type": result.get("error_type"),
        "usage": result.get("usage"),
        "elapsed_ms": result.get("elapsed_ms"),
        "measured_at": result.get("measured_at"),
        "timezone": result.get("timezone") or "Asia/Shanghai",
    }
    out = ROOT / "reports" / "stage5_q041.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    home = prepare_stage5_home()
    q041 = run_q041_first()
    print(
        json.dumps(
            {
                "phase": "q041",
                "ok": q041.get("ok"),
                "meets_expected": (q041.get("review") or {}).get("meets_expected"),
                "points": (q041.get("review") or {}).get("required_points"),
                "answer": q041.get("answer"),
                "error_type": q041.get("error_type"),
                "policy_qa_home": str(home),
                "report": "reports/stage5_q041.json",
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    dev = run_generation_split("dev", out_path=ROOT / "reports" / "stage5_dev_citation.json")
    summary = {
        "ok": bool(q041.get("ok") and dev.get("ok")),
        "policy_qa_home": str(home),
        "q041_ok": q041.get("ok"),
        "q041_report": "reports/stage5_q041.json",
        "dev_generation_ok": dev.get("ok"),
        "dev_report": "reports/stage5_dev_citation.json",
        "citation": (dev.get("summary") or {}).get("citation"),
        "answer_quality": (dev.get("summary") or {}).get("answer_quality"),
        "refusal": (dev.get("summary") or {}).get("refusal"),
        "n_generation_failed": (dev.get("summary") or {}).get("n_generation_failed"),
        "generation_failed_ids": (dev.get("summary") or {}).get("generation_failed_ids"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
