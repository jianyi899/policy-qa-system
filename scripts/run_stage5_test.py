#!/usr/bin/env python3
"""测试集第二次跑分：引用与冲突模块收尾之后只跑一次。数字不参与调参。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compare import compare_per_question  # noqa: E402
from eval.seal import verify_testset_seal  # noqa: E402
from scripts.eval_retrieval import evaluate  # noqa: E402
from scripts.stage5_common import prepare_stage5_home  # noqa: E402
from scripts.stage5_eval import run_generation_split  # noqa: E402


def main() -> int:
    home = prepare_stage5_home()
    seal = verify_testset_seal()
    if not seal.get("ok"):
        print(json.dumps({"ok": False, "error_type": "testset_seal_mismatch", "seal": seal}, ensure_ascii=False, indent=2))
        return 6

    retrieval = evaluate("test", retriever="hybrid")
    retrieval["testset_seal"] = seal
    retrieval_eval_path = ROOT / "reports" / "test_run2_retrieval_eval.json"
    retrieval_eval_path.write_text(json.dumps(retrieval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    run1 = json.loads((ROOT / "reports" / "test_run1_eval.json").read_text(encoding="utf-8"))
    comparison = compare_per_question(
        run1.get("per_question") or [],
        retrieval.get("per_question") or [],
        baseline_name="test_run1_retrieval",
        current_name="test_run2_retrieval",
    )

    generation = run_generation_split("test", out_path=ROOT / "reports" / "test_run2_generation.json")

    run1_summary = json.loads((ROOT / "reports" / "test_run1.json").read_text(encoding="utf-8"))
    report = {
        "ok": bool(retrieval.get("ok") and generation.get("ok") and seal.get("ok")),
        "name": "test_run2",
        "status": "measured",
        "split": "test",
        "policy_qa_home": str(home),
        "note": (
            "第二次跑测试集。在句级引用与冲突消解模块收尾之后进行。数字不参与调参，也不据此回改链路。"
            "第一次测试集只评检索，故检索指标与 test_run1 对比；生成/引用/拒答/答题质量为首次实测，口径不同，不能与第一次硬比。"
        ),
        "testset_seal": seal,
        "model": "qwen3.8-flash",
        "cascade_note": "级联阈值尚未标定，默认不升级（reason=no_threshold_always_fast）",
        "retrieval": {
            "metrics": retrieval.get("metrics"),
            "Recall@5": (retrieval.get("metrics") or {}).get("Recall@5") or retrieval.get("Recall@5"),
            "Hit@5": (retrieval.get("metrics") or {}).get("Hit@5") or retrieval.get("Hit@5"),
            "by_type": retrieval.get("by_type"),
            "n_used_for_recall": retrieval.get("n_used_for_recall"),
            "n_failures_hit@5": retrieval.get("n_failures_hit@5"),
            "failure_ids": retrieval.get("failure_ids"),
            "per_question_path": "reports/test_run2_retrieval_eval.json",
            "vs_test_run1": comparison,
            "test_run1_recall@5": (run1_summary.get("metrics") or {}).get("Recall@5"),
            "test_run1_hit@5": (run1_summary.get("metrics") or {}).get("Hit@5"),
        },
        "generation": {
            "model": "qwen3.8-flash",
            "summary": generation.get("summary"),
            "q042": generation.get("q042"),
            "per_question_path": "reports/test_run2_generation.json",
            "vs_test_run1": "第一次测试集未跑生成、引用与拒答，本项无对照，不可与检索指标直接比较。",
        },
        "citation": (generation.get("summary") or {}).get("citation"),
        "answer_quality": (generation.get("summary") or {}).get("answer_quality"),
        "refusal": (generation.get("summary") or {}).get("refusal"),
    }
    out = ROOT / "reports" / "test_run2.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": report["ok"],
        "report": str(out),
        "seal": {
            "ok": seal.get("ok"),
            "expected_sha256": seal.get("expected_sha256"),
            "observed_sha256": seal.get("observed_sha256"),
        },
        "retrieval_recall@5": report["retrieval"].get("Recall@5"),
        "retrieval_hit@5": report["retrieval"].get("Hit@5"),
        "vs_test_run1": {
            "wrong_to_right": comparison.get("wrong_to_right"),
            "right_to_wrong": comparison.get("right_to_wrong"),
            "mcnemar": comparison.get("mcnemar"),
        },
        "citation": report.get("citation"),
        "answer_quality": report.get("answer_quality"),
        "refusal": report.get("refusal"),
        "q042": ((generation.get("q042") or {}).get("review") or {}),
        "generation_failed_ids": (generation.get("summary") or {}).get("generation_failed_ids"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
