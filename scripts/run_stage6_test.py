#!/usr/bin/env python3
"""测试集第三次（交付前）跑分：只跑一次。数字不参与调参，也不据此回改链路。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compare import compare_per_question, mcnemar_exact  # noqa: E402
from app.quality import score_task  # noqa: E402
from eval.seal import verify_testset_seal  # noqa: E402
from scripts.eval_retrieval import evaluate  # noqa: E402
from scripts.stage5_eval import run_generation_split  # noqa: E402

DEFAULT_HOME = Path("/tmp/policy-qa-stage6-test")
FORBIDDEN_HOMES = {
    Path("/d/policy-qa"),
    Path("D:/policy-qa"),
    Path("D:\\policy-qa"),
    Path.home() / "policy-qa",
}


def prepare_stage6_home() -> Path:
    """固定使用本段测试目录，避免写到 D:/policy-qa 或已有 Postgres/Redis 数据。"""
    from dotenv import load_dotenv

    from app.config import clear_settings_cache

    load_dotenv(ROOT / ".env", override=False)
    raw = os.environ.get("POLICY_QA_HOME", "").strip()
    target = Path(raw).expanduser() if raw else DEFAULT_HOME
    resolved = target.resolve() if target.exists() or target.is_absolute() else target
    forbidden = {p.expanduser().resolve() if p.exists() else p.expanduser() for p in FORBIDDEN_HOMES}
    if resolved in forbidden or str(target).replace("\\", "/").rstrip("/").lower() in {
        "d:/policy-qa",
        str((Path.home() / "policy-qa").as_posix()),
    }:
        raise SystemExit(
            f"拒绝使用 {target}：第六段评测必须在 {DEFAULT_HOME} 进行，"
            "不要改 D:/policy-qa，也不要动本机已有 Postgres/Redis。"
        )
    os.environ["POLICY_QA_HOME"] = str(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / "data").mkdir(parents=True, exist_ok=True)
    (target / "logs").mkdir(parents=True, exist_ok=True)
    clear_settings_cache()
    return target


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
            "用已保存回答重算 test_run2，未重跑第二次生成。本对照不参与调参。"
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
        if cascade.get("upgraded") or item.get("cascaded"):
            n_upgraded += 1
    return {
        "n_items": len(items),
        "n_with_cascade": n_with,
        "n_upgraded_to_plus": n_upgraded,
        "by_model": models,
        "note": "级联阈值尚未标定，默认不升级（reason=no_threshold_always_fast）。未编造阈值。",
    }


def main() -> int:
    home = prepare_stage6_home()
    seal = verify_testset_seal()
    if not seal.get("ok"):
        print(
            json.dumps(
                {"ok": False, "error_type": "testset_seal_mismatch", "seal": seal},
                ensure_ascii=False,
                indent=2,
            )
        )
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
        model_label="qwen3.8-flash",
    )

    questions = _questions_by_id()
    run2_gen = json.loads((ROOT / "reports" / "test_run2_generation.json").read_text(encoding="utf-8"))
    vs_run2_gen = compare_generation(
        run2_gen.get("items") or [],
        generation.get("items") or [],
        questions,
    )
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
        "policy_qa_home": str(home),
        "note": (
            "第三次也是交付前最后一次跑测试集。在独立测试目录实测生成、引用、检索与拒答。"
            "数字不参与调参，也不据此回改链路或拒答启发式。"
            "封存 sha256 与前两次对照。检索与 test_run1 / test_run2 对比；"
            "生成/引用/拒答/答题质量与 test_run2 对比（第一次只评检索，不能与之硬比）。"
            "级联阈值尚未标定，生成仍默认 qwen3.8-flash，不升级到 qwen-plus。"
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
            "per_question_path": "reports/test_run3_retrieval_eval.json",
            "vs_test_run1": vs_run1,
            "vs_test_run2": vs_run2_ret,
            "test_run1_recall@5": (run1_summary.get("metrics") or {}).get("Recall@5"),
            "test_run1_hit@5": (run1_summary.get("metrics") or {}).get("Hit@5"),
            "test_run2_recall@5": ((run2_summary.get("retrieval") or {}).get("Recall@5")),
            "test_run2_hit@5": ((run2_summary.get("retrieval") or {}).get("Hit@5")),
        },
        "generation": {
            "model": "qwen3.8-flash",
            "cascade_stats": _cascade_stats(generation.get("items") or []),
            "summary": generation.get("summary"),
            "q042": generation.get("q042"),
            "per_question_path": "reports/test_run3_generation.json",
            "vs_test_run1": "第一次测试集未跑生成、引用与拒答，本项无对照，不可与检索指标直接比较。",
            "vs_test_run2": vs_run2_gen_summary,
        },
        "citation": (generation.get("summary") or {}).get("citation"),
        "answer_quality": (generation.get("summary") or {}).get("answer_quality"),
        "refusal": (generation.get("summary") or {}).get("refusal"),
    }
    out = ROOT / "reports" / "test_run3.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": report["ok"],
        "report": str(out),
        "policy_qa_home": str(home),
        "seal": {
            "ok": seal.get("ok"),
            "expected_sha256": seal.get("expected_sha256"),
            "observed_sha256": seal.get("observed_sha256"),
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
        "answer_quality": report.get("answer_quality"),
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
