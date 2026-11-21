"""第五段评测共用逻辑：逐题生成、引用精度汇总、答题质量与拒答统计。测试集数字不得用于调参。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from app.answer_check import check_q041_answer, check_q042_answer, check_refusal, is_refusal_answer
from app.dialogue import answer_question
from app.quality import score_task

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


def compact_citations(citations: list[dict] | None) -> list[dict]:
    out = []
    for c in citations or []:
        out.append(
            {
                "sid": c.get("sid"),
                "valid_evidence": c.get("valid_evidence"),
                "doc_id": c.get("doc_id"),
                "section_path": c.get("section_path"),
                "char_start": c.get("char_start"),
                "char_end": c.get("char_end"),
                "text": c.get("text"),
            }
        )
    return out


def evaluate_generated(question: dict, result: dict) -> dict:
    answer = result.get("answer") or ""
    should_refuse = bool(question.get("should_refuse"))
    refusal = check_refusal(answer, should_refuse=should_refuse)
    citation_score = result.get("citation_score")
    task = None
    if result.get("ok"):
        task = score_task(
            answer=answer,
            should_refuse=should_refuse,
            answer_points=question.get("answer_points"),
            citation_score=citation_score,
        )
    q041 = check_q041_answer(answer) if question["id"] == "Q041" else None
    q042 = check_q042_answer(answer) if question["id"] == "Q042" else None
    return {
        "id": question["id"],
        "type": question.get("type"),
        "cluster": question.get("cluster"),
        "should_refuse": should_refuse,
        "ok_generation": bool(result.get("ok")),
        "error_type": result.get("error_type"),
        "answer": answer if result.get("ok") else None,
        "model": result.get("model"),
        "cascaded": result.get("cascaded"),
        "cascade": result.get("cascade"),
        "usage": result.get("usage"),
        "elapsed_ms": result.get("elapsed_ms"),
        "citations": compact_citations(result.get("citations")),
        "citation_score": (
            {
                "n_cited": citation_score.get("n_cited"),
                "n_correct": citation_score.get("n_correct"),
                "n_incorrect": citation_score.get("n_incorrect"),
                "precision": citation_score.get("precision"),
                "n_gold_sentences": citation_score.get("n_gold_sentences"),
                "n_gold_hit": citation_score.get("n_gold_hit"),
                "recall": citation_score.get("recall"),
                "definition": citation_score.get("definition"),
            }
            if citation_score
            else None
        ),
        "task": (
            {
                "task_ok": task.get("task_ok"),
                "looks_like_refusal": task.get("looks_like_refusal"),
                "point_coverage": task.get("point_coverage"),
                "citation_recall": task.get("citation_recall"),
                "citation_precision": task.get("citation_precision"),
            }
            if task
            else None
        ),
        "refusal": refusal,
        "q041": q041,
        "q042": q042,
        "conflict_has_finer_scope": (result.get("conflict_resolution") or {}).get("has_finer_scope"),
        "conflict_explanation": (result.get("conflict_resolution") or {}).get("explanation"),
        "by_scope_doc_ids": (result.get("conflict_resolution") or {}).get("by_scope_doc_ids"),
        "looks_like_refusal": is_refusal_answer(answer),
    }


def aggregate(items: list[dict], *, split: str) -> dict:
    answerable = [x for x in items if not x.get("should_refuse")]
    refusals = [x for x in items if x.get("should_refuse")]
    gen_ok = [x for x in items if x.get("ok_generation")]
    gen_fail = [x for x in items if not x.get("ok_generation")]

    precisions = []
    precisions_missing_as_zero = []
    recalls = []
    micro_correct = 0
    micro_cited = 0
    n_with_cite = 0
    n_missing_cite = 0
    task_ok_ids = []
    task_fail_ids = []
    point_recalls = []
    for x in answerable:
        if not x.get("ok_generation"):
            continue
        score = x.get("citation_score") or {}
        p = score.get("precision")
        if p is None:
            n_missing_cite += 1
            precisions_missing_as_zero.append(0.0)
        else:
            n_with_cite += 1
            precisions.append(p)
            precisions_missing_as_zero.append(p)
        if score.get("recall") is not None:
            recalls.append(score["recall"])
        micro_cited += int(score.get("n_cited") or 0)
        micro_correct += int(score.get("n_correct") or 0)
        task = x.get("task") or {}
        if task.get("task_ok"):
            task_ok_ids.append(x["id"])
        else:
            task_fail_ids.append(x["id"])
        cov = (task.get("point_coverage") or {}).get("recall")
        if cov is not None:
            point_recalls.append(cov)

    refusal_ok = [x for x in refusals if x.get("ok_generation") and (x.get("refusal") or {}).get("ok")]
    refusal_fail = [
        x["id"]
        for x in refusals
        if x.get("ok_generation") and not (x.get("refusal") or {}).get("ok")
    ]
    refusal_errors = [x["id"] for x in refusals if not x.get("ok_generation")]

    def _mean(xs: list[float]) -> float | None:
        return (sum(xs) / len(xs)) if xs else None

    n_answerable_generated = sum(1 for x in answerable if x.get("ok_generation"))
    return {
        "split": split,
        "n_questions": len(items),
        "n_answerable": len(answerable),
        "n_refusal": len(refusals),
        "n_generation_ok": len(gen_ok),
        "n_generation_failed": len(gen_fail),
        "generation_failed_ids": [x["id"] for x in gen_fail],
        "citation": {
            "metric": "sentence_precision",
            "definition": (
                "按句子算引用精度。系统回答中每个 [S#] 映射到证据句（doc_id+section_path+字符起止，"
                "与条款标注同一 documents.body 坐标系）；该句与任一金标条款跨度在同一章节下"
                "字符交集长度>0 视为正确。不对着检索改标注。可答题、生成成功的题目上统计。"
            ),
            "n_answerable_generated": n_answerable_generated,
            "n_with_citations": n_with_cite,
            "n_without_citations": n_missing_cite,
            "macro_precision_over_questions_with_citations": _mean(precisions),
            "macro_precision_missing_as_zero": _mean(precisions_missing_as_zero),
            "macro_recall_over_questions_with_gold_sentences": _mean(recalls),
            "micro_precision": (micro_correct / micro_cited) if micro_cited else None,
            "micro_n_correct": micro_correct,
            "micro_n_cited": micro_cited,
        },
        "answer_quality": {
            "definition": (
                "可答题：不得拒答，且至少引用到一条金标句或覆盖至少一条金标要点；"
                "要点覆盖见 app.quality.point_coverage。拒答另计。"
            ),
            "n_answerable_generated": n_answerable_generated,
            "n_task_ok": len(task_ok_ids),
            "n_task_fail": len(task_fail_ids),
            "task_ok_over_generated": f"{len(task_ok_ids)}/{n_answerable_generated}",
            "task_fail_ids": task_fail_ids,
            "macro_point_coverage": _mean(point_recalls),
        },
        "refusal": {
            "n": len(refusals),
            "n_evaluated": len(refusals) - len(refusal_errors),
            "n_correct": len(refusal_ok),
            "n_incorrect": len(refusal_fail),
            "n_generation_failed": len(refusal_errors),
            "correct_over_total": f"{len(refusal_ok)}/{len(refusals)}",
            "incorrect_ids": refusal_fail,
            "generation_failed_ids": refusal_errors,
            "note": "小样本只报对了几道/共几道，不设 90% 目标。",
        },
    }


def run_generation_split(
    split: str,
    *,
    out_path: Path,
    pause_s: float = 0.8,
    ids: list[str] | None = None,
    model_label: str | None = None,
) -> dict:
    questions = load_questions(split)
    if ids is not None:
        wanted = set(ids)
        questions = [q for q in questions if q["id"] in wanted]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = out_path.with_suffix(out_path.suffix + ".partial")
    done: dict[str, dict] = {}
    if partial_path.is_file():
        try:
            prev = json.loads(partial_path.read_text(encoding="utf-8"))
            for item in prev.get("items") or []:
                if item.get("ok_generation"):
                    done[item["id"]] = item
        except Exception:  # noqa: BLE001
            done = {}

    items: list[dict] = []
    for i, q in enumerate(questions, start=1):
        if q["id"] in done:
            items.append(done[q["id"]])
            print(
                json.dumps(
                    {"progress": f"{i}/{len(questions)}", "id": q["id"], "ok": True, "cached": True},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue
        gold = q.get("relevant_spans") or []
        result = answer_question(q["question"], gold_spans=gold)
        item = evaluate_generated(q, result)
        items.append(item)
        done[q["id"]] = item
        payload = {"split": split, "items": items, "n_done": len(items), "n_total": len(questions)}
        partial_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "progress": f"{i}/{len(questions)}",
                    "id": q["id"],
                    "ok": item.get("ok_generation"),
                    "error_type": item.get("error_type"),
                    "n_cited": (item.get("citation_score") or {}).get("n_cited"),
                    "precision": (item.get("citation_score") or {}).get("precision"),
                    "task_ok": (item.get("task") or {}).get("task_ok"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if pause_s and i < len(questions):
            time.sleep(pause_s)

    summary = aggregate(items, split=split)
    report = {
        "ok": summary["n_generation_failed"] == 0,
        "split": split,
        "model": model_label or "qwen3.8-flash",
        "cascade_note": "级联阈值尚未标定，默认不升级（reason=no_threshold_always_fast）",
        "summary": summary,
        "items": items,
    }
    for item in items:
        if item["id"] == "Q041":
            report["q041"] = {
                "answer": item.get("answer"),
                "review": item.get("q041"),
                "citations": item.get("citations"),
                "conflict_explanation": item.get("conflict_explanation"),
            }
        if item["id"] == "Q042":
            report["q042"] = {
                "answer": item.get("answer"),
                "review": item.get("q042"),
                "citations": item.get("citations"),
                "conflict_explanation": item.get("conflict_explanation"),
            }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if partial_path.is_file():
        partial_path.unlink()
    return report
