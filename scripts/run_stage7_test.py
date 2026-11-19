#!/usr/bin/env python3
"""测试集第三次（最终）跑分：交付收尾后只跑一次。数字不参与调参。

检索与 test_run1 / test_run2 对照；生成/引用/拒答与 test_run2 对照
（第一次测试集只评了检索）。生成走产品路径：qwen3.8-flash，
按已标定阈值级联升至 qwen-plus；评测时关闭问答缓存，避免原句命中干扰对照。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compare import compare_per_question, mcnemar_exact  # noqa: E402
from app.quality import score_task  # noqa: E402
from eval.seal import verify_testset_seal  # noqa: E402
from scripts.eval_retrieval import evaluate  # noqa: E402
from scripts.stage5_eval import run_generation_split  # noqa: E402


def _questions_by_id() -> dict[str, dict]:
    items = []
    for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return {q["id"]: q for q in items}


def _task_score(item: dict, question: dict | None) -> dict:
    q = question or {}
    return score_task(
        answer=item.get("answer") or "",
        should_refuse=bool(item.get("should_refuse") or q.get("should_refuse")),
        answer_points=q.get("answer_points"),
        citation_score=item.get("citation_score"),
    )


def compare_generation(baseline: list[dict], current: list[dict], questions: dict[str, dict]) -> dict:
    """以 task_ok 作为对/错：拒答题看是否正确拒答；可答题看要点或金标句是否命中。"""
    base_map = {r["id"]: r for r in baseline}
    curr_map = {r["id"]: r for r in current}
    ids = sorted(set(base_map) & set(curr_map))
    improved_ids: list[str] = []
    regressed_ids: list[str] = []
    per_question = []
    for qid in ids:
        b = base_map[qid]
        c = curr_map[qid]
        q = questions.get(qid) or {}
        b_score = _task_score(b, q)
        c_score = _task_score(c, q)
        b_ok = bool(b.get("ok_generation")) and bool(b_score["task_ok"])
        c_ok = bool(c.get("ok_generation")) and bool(c_score["task_ok"])
        if (not b_ok) and c_ok:
            improved_ids.append(qid)
        elif b_ok and (not c_ok):
            regressed_ids.append(qid)
        per_question.append(
            {
                "id": qid,
                "should_refuse": bool(c.get("should_refuse") or q.get("should_refuse")),
                "baseline_ok": b_ok,
                "current_ok": c_ok,
                "baseline_task": b_score,
                "current_task": c_score,
            }
        )
    mcn = mcnemar_exact(len(improved_ids), len(regressed_ids))
    n_base_ok = sum(1 for x in per_question if x["baseline_ok"])
    n_curr_ok = sum(1 for x in per_question if x["current_ok"])
    refusals = [x for x in per_question if x["should_refuse"]]
    answerable = [x for x in per_question if not x["should_refuse"]]
    return {
        "baseline": "test_run2_generation",
        "current": "test_run3_generation",
        "success_definition": (
            "task_ok：拒答题须正确拒答；可答题不得拒答，且至少引用到一条金标句或覆盖至少一条金标要点。"
            "该口径与第六段开发集级联标定相同。用已保存回答重算 test_run2，未重跑生成。"
        ),
        "n_comparable": len(ids),
        "n_only_baseline": len(set(base_map) - set(curr_map)),
        "n_only_current": len(set(curr_map) - set(base_map)),
        "wrong_to_right": {"n": len(improved_ids), "ids": improved_ids},
        "right_to_wrong": {"n": len(regressed_ids), "ids": regressed_ids},
        "mcnemar": mcn,
        "task_ok": {
            "baseline": f"{n_base_ok}/{len(ids)}",
            "current": f"{n_curr_ok}/{len(ids)}",
            "answerable_baseline": f"{sum(1 for x in answerable if x['baseline_ok'])}/{len(answerable)}",
            "answerable_current": f"{sum(1 for x in answerable if x['current_ok'])}/{len(answerable)}",
            "refusal_baseline": f"{sum(1 for x in refusals if x['baseline_ok'])}/{len(refusals)}",
            "refusal_current": f"{sum(1 for x in refusals if x['current_ok'])}/{len(refusals)}",
        },
        "per_question": per_question,
    }


def _cascade_stats(items: list[dict]) -> dict:
    models: dict[str, int] = {}
    n_upgraded = 0
    n_with = 0
    for item in items:
        cascade = item.get("cascade") or {}
        n_with += 1 if cascade else 0
        model = item.get("model") or cascade.get("model")
        if model:
            models[model] = models.get(model, 0) + 1
        if cascade.get("upgraded"):
            n_upgraded += 1
    return {
        "n_items": len(items),
        "n_with_cascade": n_with,
        "n_upgraded_to_plus": n_upgraded,
        "by_model": models,
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    seal = verify_testset_seal()
    if not seal.get("ok"):
        print(json.dumps({"ok": False, "error_type": "testset_seal_mismatch", "seal": seal}, ensure_ascii=False, indent=2))
        return 6

    retrieval = evaluate("test", retriever="hybrid")
    retrieval["testset_seal"] = seal
    retrieval_eval_path = ROOT / "reports" / "test_run3_retrieval_eval.json"
    retrieval_eval_path.write_text(json.dumps(retrieval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    run1 = json.loads((ROOT / "reports" / "test_run1_eval.json").read_text(encoding="utf-8"))
    run2_retrieval = json.loads((ROOT / "reports" / "test_run2_retrieval_eval.json").read_text(encoding="utf-8"))
    vs_run1 = compare_per_question(
        run1.get("per_question") or [],
        retrieval.get("per_question") or [],
        baseline_name="test_run1_retrieval",
        current_name="test_run3_retrieval",
    )
    vs_run2_ret = compare_per_question(
        run2_retrieval.get("per_question") or [],
        retrieval.get("per_question") or [],
        baseline_name="test_run2_retrieval",
        current_name="test_run3_retrieval",
    )

    generation = run_generation_split(
        "test",
        out_path=ROOT / "reports" / "test_run3_generation.json",
        use_cascade=True,
        use_cache=False,
        model_label="qwen3.8-flash（级联升 qwen-plus）",
    )

    questions = _questions_by_id()
    run2_gen = json.loads((ROOT / "reports" / "test_run2_generation.json").read_text(encoding="utf-8"))
    vs_run2_gen = compare_generation(
        run2_gen.get("items") or [],
        generation.get("items") or [],
        questions,
    )
    # 对照表写入独立附件，避免把逐题 task 细节全部堆进主报告。
    gen_cmp_path = ROOT / "reports" / "test_run3_generation_vs_run2.json"
    gen_cmp_path.write_text(json.dumps(vs_run2_gen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    vs_run2_gen_summary = {k: v for k, v in vs_run2_gen.items() if k != "per_question"}
    vs_run2_gen_summary["per_question_path"] = "reports/test_run3_generation_vs_run2.json"

    run1_summary = json.loads((ROOT / "reports" / "test_run1.json").read_text(encoding="utf-8"))
    run2_summary = json.loads((ROOT / "reports" / "test_run2.json").read_text(encoding="utf-8"))
    report = {
        "ok": bool(retrieval.get("ok") and generation.get("ok") and seal.get("ok")),
        "name": "test_run3",
        "status": "measured",
        "split": "test",
        "note": (
            "第三次也是最终一次跑测试集，在 README 对比表与演示收尾之后进行。数字不参与调参。"
            "封存 sha256 与前两次对照。检索与 test_run1 / test_run2 对比；"
            "生成/引用/拒答与 test_run2 对比（第一次只评检索）。"
            "生成走产品路径：use_cascade=True（qwen3.8-flash 按已标定阈值升 qwen-plus），"
            "use_cache=False，以免原句缓存命中干扰对照。"
        ),
        "testset_seal": seal,
        "model": generation.get("model"),
        "use_cascade": True,
        "use_cache": False,
        "retrieval": {
            "metrics": retrieval.get("metrics"),
            "Recall@5": retrieval.get("metrics", {}).get("Recall@5") or retrieval.get("Recall@5"),
            "Hit@5": retrieval.get("metrics", {}).get("Hit@5") or retrieval.get("Hit@5"),
            "by_type": retrieval.get("by_type"),
            "n_used_for_recall": retrieval.get("n_used_for_recall"),
            "n_failures_hit@5": retrieval.get("n_failures_hit@5"),
            "failure_ids": retrieval.get("failure_ids"),
            "per_question_path": "reports/test_run3_retrieval_eval.json",
            "vs_test_run1": vs_run1,
            "vs_test_run2": vs_run2_ret,
            "test_run1_recall@5": (run1_summary.get("metrics") or {}).get("Recall@5"),
            "test_run1_hit@5": (run1_summary.get("metrics") or {}).get("Hit@5"),
            "test_run2_recall@5": ((run2_summary.get("retrieval") or {}).get("Recall@5")),
            "test_run2_hit@5": ((run2_summary.get("retrieval") or {}).get("Hit@5")),
        },
        "generation": {
            "model": generation.get("model"),
            "use_cascade": True,
            "use_cache": False,
            "cascade_stats": _cascade_stats(generation.get("items") or []),
            "summary": generation.get("summary"),
            "q042": generation.get("q042"),
            "per_question_path": "reports/test_run3_generation.json",
            "vs_test_run1": "第一次测试集未跑生成，本项无对照。",
            "vs_test_run2": vs_run2_gen_summary,
        },
        "citation": (generation.get("summary") or {}).get("citation"),
        "refusal": (generation.get("summary") or {}).get("refusal"),
    }
    out = ROOT / "reports" / "test_run3.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": report["ok"],
        "report": str(out),
        "seal": {
            "ok": seal.get("ok"),
            "sha256": seal.get("observed_sha256") or seal.get("expected_sha256"),
            "match": seal.get("match"),
        },
        "retrieval_recall@5": report["retrieval"].get("Recall@5"),
        "retrieval_hit@5": report["retrieval"].get("Hit@5"),
        "vs_test_run1_retrieval": {
            "wrong_to_right": vs_run1.get("wrong_to_right"),
            "right_to_wrong": vs_run1.get("right_to_wrong"),
            "mcnemar": vs_run1.get("mcnemar"),
        },
        "vs_test_run2_retrieval": {
            "wrong_to_right": vs_run2_ret.get("wrong_to_right"),
            "right_to_wrong": vs_run2_ret.get("right_to_wrong"),
            "mcnemar": vs_run2_ret.get("mcnemar"),
        },
        "citation": report.get("citation"),
        "refusal": report.get("refusal"),
        "cascade_stats": report["generation"].get("cascade_stats"),
        "vs_test_run2_generation": vs_run2_gen_summary,
        "q042": ((generation.get("q042") or {}).get("review") or {}),
        "generation_failed_ids": (generation.get("summary") or {}).get("generation_failed_ids"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
