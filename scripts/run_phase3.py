#!/usr/bin/env python3
"""第三段编排：三种切分消融、BM25+RRF、gte-rerank、CI 基线，以及第一次测试集评测。

选型与调参只用开发集。测试集只在锁定最终检索链路之后跑一次，数字不参与调参。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
REPORTS = ROOT / "reports"
SHANGHAI = timezone(timedelta(hours=8))

sys.path.insert(0, str(ROOT))

from app.chunking import (  # noqa: E402
    ABLATION_STRATEGY_NAMES,
    chunk_key,
    chunk_sections,
    get_strategy,
    params_dict,
)
from app.compare import compare_per_question  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.corpus_io import load_corpus  # noqa: E402
from app.embed_cache import ingest_pairs  # noqa: E402
from app.index_store import dump_embeddings_for_cache  # noqa: E402
from app.pipeline import LOCKED_PATH  # noqa: E402
from eval.seal import EXPECTED_SHA256, verify_testset_seal  # noqa: E402

import psycopg  # noqa: E402


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def sanitize(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"sk-[A-Za-z0-9]{8,}", "sk-[REDACTED]", text)
    text = re.sub(
        r"(DASHSCOPE_API_KEY|DEEPSEEK_API_KEY|API[_-]?KEY)([=:\s\"]+)(\S+)",
        r"\1\2[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text


def run_script(script: str, *args: str, out: Path | None = None) -> dict:
    cmd = [str(PYTHON), str(ROOT / "scripts" / script), *args]
    if out is not None:
        cmd += ["--out", str(out)]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        err: dict = {
            "ok": False,
            "error_type": "subprocess_failed",
            "script": script,
            "returncode": proc.returncode,
        }
        for stream in (proc.stdout, proc.stderr):
            if not stream or not stream.strip():
                continue
            # 子进程可能先打印报告再以非 0 退出；优先解析最后一段 JSON。
            try:
                payload = json.loads(stream[stream.rfind("{") :])
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict):
                err["error_type"] = payload.get("error_type") or err["error_type"]
                err["child"] = {k: v for k, v in payload.items() if k != "per_question"}
                break
        if "child" not in err:
            err["stdout_tail"] = sanitize((proc.stdout or "")[-2000:])
            err["stderr_tail"] = sanitize((proc.stderr or "")[-2000:])
        return err
    if out is not None and out.is_file():
        return json.loads(out.read_text(encoding="utf-8"))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error_type": "invalid_script_json",
            "script": script,
            "stdout_tail": sanitize((proc.stdout or "")[-2000:]),
        }


def fail_and_write(report: dict, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if not report.get("ok") else 0


def compact_metrics(eval_report: dict) -> dict:
    return {
        "ok": eval_report.get("ok"),
        "split": eval_report.get("split"),
        "retriever": eval_report.get("retriever"),
        "n_questions_in_split": eval_report.get("n_questions_in_split"),
        "n_answerable": eval_report.get("n_answerable"),
        "n_refusal_excluded": eval_report.get("n_refusal_excluded"),
        "n_used_for_recall": eval_report.get("n_used_for_recall"),
        "metrics": eval_report.get("metrics"),
        "Recall@5": (eval_report.get("metrics") or {}).get("Recall@5"),
        "Hit@5": (eval_report.get("metrics") or {}).get("Hit@5"),
        "by_type": eval_report.get("by_type"),
        "n_failures_hit@5": eval_report.get("n_failures_hit@5"),
        "failure_ids": eval_report.get("failure_ids"),
        "settings": eval_report.get("settings"),
        "metric_definitions": eval_report.get("metric_definitions"),
        "refusal": eval_report.get("refusal"),
        "testset_seal": eval_report.get("testset_seal"),
    }


def recall_mean(eval_report: dict) -> float | None:
    rec = (eval_report.get("metrics") or {}).get("Recall@5") or {}
    return rec.get("mean")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    # 1) 把当前库里已有的 B0 向量灌进缓存，避免重复嵌入。
    settings = get_settings()
    with psycopg.connect(settings.dsn()) as conn:
        n_cached = ingest_pairs(dump_embeddings_for_cache(conn))

    docs = load_corpus()
    chunk_sets: dict[str, set[tuple]] = {}
    ablation_rows = []
    eval_by_name: dict[str, dict] = {}
    map_by_name: dict[str, dict] = {}

    for name in ABLATION_STRATEGY_NAMES:
        params = get_strategy(name)
        chunks = chunk_sections(docs, params)
        keys = {chunk_key(c) for c in chunks}
        chunk_sets[name] = keys
        chunk_report = run_script("chunk_and_embed.py", "--strategy", name)
        if not chunk_report.get("ok"):
            return fail_and_write(
                {"ok": False, "stage": "chunk_and_embed", "strategy": name, "error": chunk_report},
                REPORTS / "phase3_summary.json",
            )
        map_path = REPORTS / f"mapping_{name}.json"
        map_report = run_script("map_annotations.py", "--out", str(map_path))
        # map_annotations 把摘要打到 stdout，完整映射在 --out 文件。
        if map_path.is_file():
            full_map = json.loads(map_path.read_text(encoding="utf-8"))
        else:
            full_map = map_report
        if not map_report.get("ok") and map_report.get("error_type"):
            return fail_and_write(
                {"ok": False, "stage": "map_annotations", "strategy": name, "error": map_report},
                REPORTS / "phase3_summary.json",
            )
        # 即便 ok=False（有未映射跨度）也继续实测，但必须写进报告。
        eval_path = REPORTS / f"chunking_{name}_dev_eval.json"
        eval_report = run_script(
            "eval_retrieval.py",
            "--split",
            "dev",
            "--retriever",
            "dense",
            "--mapping",
            str(map_path),
            out=eval_path,
        )
        if not eval_report.get("ok"):
            return fail_and_write(
                {"ok": False, "stage": "chunking_eval", "strategy": name, "error": eval_report},
                REPORTS / "phase3_summary.json",
            )
        eval_by_name[name] = eval_report
        map_by_name[name] = full_map
        row = {
            "name": name,
            "chunking": params_dict(params, name=name),
            "n_chunks": len(chunks),
            "n_unmapped_spans": full_map.get("n_unmapped_spans"),
            "dev_answerable": full_map.get("dev_answerable"),
            "metrics": eval_report.get("metrics"),
            "Recall@5": (eval_report.get("metrics") or {}).get("Recall@5"),
            "Hit@5": (eval_report.get("metrics") or {}).get("Hit@5"),
            "n_failures_hit@5": eval_report.get("n_failures_hit@5"),
            "failure_ids": eval_report.get("failure_ids"),
            "per_question_path": str(eval_path.relative_to(ROOT)),
        }
        ablation_rows.append(row)

    # 确认三种策略切片集互不相同。
    distinct = True
    pairwise = []
    names = list(ABLATION_STRATEGY_NAMES)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            same = chunk_sets[a] == chunk_sets[b]
            if same:
                distinct = False
            pairwise.append(
                {
                    "a": a,
                    "b": b,
                    "n_a": len(chunk_sets[a]),
                    "n_b": len(chunk_sets[b]),
                    "intersection": len(chunk_sets[a] & chunk_sets[b]),
                    "only_a": len(chunk_sets[a] - chunk_sets[b]),
                    "only_b": len(chunk_sets[b] - chunk_sets[a]),
                    "equal": same,
                }
            )
    if not distinct:
        return fail_and_write(
            {"ok": False, "error_type": "chunking_strategies_not_distinct", "pairwise": pairwise},
            REPORTS / "phase3_summary.json",
        )

    baseline_name = "section_window_400"
    comparisons = {}
    for row in ablation_rows:
        name = row["name"]
        if name == baseline_name:
            continue
        comparisons[name] = compare_per_question(
            eval_by_name[baseline_name]["per_question"],
            eval_by_name[name]["per_question"],
            baseline_name=baseline_name,
            current_name=name,
        )
        row["vs_section_window_400"] = comparisons[name]

    # 按开发集 Recall@5 选型；并列时保留 B0 基线切分。
    def selection_key(row: dict) -> tuple:
        mean = (row.get("Recall@5") or {}).get("mean")
        if mean is None:
            mean = -1.0
        prefer_b0 = 1 if row["name"] == baseline_name else 0
        return (mean, prefer_b0)

    selected_chunking = max(ablation_rows, key=selection_key)
    selected_name = selected_chunking["name"]
    selected_reason = (
        f"按开发集纯密集检索 Recall@5 最高者锁定切分；"
        f"选定 {selected_name}，Recall@5="
        f"{(selected_chunking.get('Recall@5') or {}).get('mean')}。"
    )
    if selected_name == baseline_name:
        selected_reason += " B0 切分即为最好或并列最好，予以保留。"

    ablation_report = {
        "name": "chunking_ablation",
        "status": "measured",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "split": "dev",
        "retrieval": "dense_only",
        "embedding_model": "text-embedding-v4",
        "embedding_dim": 1024,
        "n_cached_vectors_ingested_at_start": n_cached,
        "strategies_distinct": distinct,
        "pairwise_chunk_sets": pairwise,
        "strategies": ablation_rows,
        "comparisons_vs_section_window_400": comparisons,
        "selected": selected_name,
        "selection_rule": "开发集 Recall@5 宏平均最高；并列时保留 section_window_400（B0）。测试集数字不参与选型。",
        "selection_reason": selected_reason,
    }
    write_json(REPORTS / "chunking_ablation.json", ablation_report)

    # 把选定切分写回正式索引与正式映射。
    reload = run_script("chunk_and_embed.py", "--strategy", selected_name)
    if not reload.get("ok"):
        return fail_and_write(
            {"ok": False, "stage": "reload_selected_chunking", "error": reload},
            REPORTS / "phase3_summary.json",
        )
    _official_map = run_script("map_annotations.py")
    if not _official_map.get("ok") and _official_map.get("error_type"):
        raise SystemExit(json.dumps({"ok": False, "stage": "official_map", "error": _official_map}, ensure_ascii=False))
    # 完整文件已写到 eval/annotation_mapping.json；再复制一份选定映射作为对照。
    src_map = REPORTS / f"mapping_{selected_name}.json"
    official = ROOT / "eval" / "annotation_mapping.json"
    if src_map.is_file() and official.is_file():
        pass
    dense_eval = eval_by_name[selected_name]
    dense_eval_path = REPORTS / f"chunking_{selected_name}_dev_eval.json"

    # 2) BM25 + RRF，在选定切分上对开发集实测。
    hybrid_eval_path = REPORTS / "hybrid_dev_eval.json"
    hybrid_eval = run_script(
        "eval_retrieval.py",
        "--split",
        "dev",
        "--retriever",
        "hybrid",
        "--mapping",
        str(official),
        out=hybrid_eval_path,
    )
    if not hybrid_eval.get("ok"):
        return fail_and_write(
            {"ok": False, "stage": "hybrid_eval", "error": hybrid_eval},
            REPORTS / "phase3_summary.json",
        )
    hybrid_cmp = compare_per_question(
        dense_eval["per_question"],
        hybrid_eval["per_question"],
        baseline_name=f"{selected_name}+dense",
        current_name=f"{selected_name}+hybrid",
    )
    hybrid_better = (recall_mean(hybrid_eval) or -1) > (recall_mean(dense_eval) or -1)
    # 严格更好才纳入主链路；持平或更差则不加。
    use_hybrid = hybrid_better
    hybrid_report = {
        "name": "bm25_rrf",
        "status": "measured",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "split": "dev",
        "chunking_name": selected_name,
        "dense": compact_metrics(dense_eval),
        "hybrid": compact_metrics(hybrid_eval),
        "comparison_vs_dense": hybrid_cmp,
        "adopted": use_hybrid,
        "reason": (
            "融合后开发集 Recall@5 高于纯密集，纳入主链路。"
            if use_hybrid
            else "融合后开发集 Recall@5 未高于纯密集，不纳入主链路。不为了必须提升而改参数或挪数字。"
        ),
        "per_question_path": str(hybrid_eval_path.relative_to(ROOT)),
    }
    write_json(REPORTS / "hybrid_dev.json", hybrid_report)

    # 3) 在融合检索之后做 gte-rerank，再用开发集决定是否纳入。
    # 即使融合未被单独采纳，本步仍按「融合之后 rerank」实测，再与当时最好的链路比较。
    rerank_eval_path = REPORTS / "rerank_dev_eval.json"
    rerank_eval = run_script(
        "eval_retrieval.py",
        "--split",
        "dev",
        "--retriever",
        "hybrid_rerank",
        "--mapping",
        str(official),
        out=rerank_eval_path,
    )
    rerank_report: dict
    if not rerank_eval.get("ok"):
        rerank_report = {
            "name": "gte_rerank",
            "status": "failed",
            "measured_at": now_iso(),
            "timezone": "Asia/Shanghai",
            "split": "dev",
            "chunking_name": selected_name,
            "error_type": rerank_eval.get("error_type") or (rerank_eval.get("error") or {}).get("error_type") or rerank_eval.get("error_type"),
            "error": {k: v for k, v in rerank_eval.items() if k != "per_question"},
            "adopted": False,
            "reason": "gte-rerank API 调用失败，不编造结果，本步不纳入主链路。",
        }
        write_json(REPORTS / "rerank_dev.json", rerank_report)
        rerank_eval = None
        rerank_cmp_vs_hybrid = None
        rerank_cmp_vs_best_so_far = None
        use_rerank = False
    else:
        base_for_rerank = hybrid_eval
        rerank_cmp_vs_hybrid = compare_per_question(
            hybrid_eval["per_question"],
            rerank_eval["per_question"],
            baseline_name=f"{selected_name}+hybrid",
            current_name=f"{selected_name}+hybrid_rerank",
        )
        best_so_far = hybrid_eval if use_hybrid else dense_eval
        best_so_far_name = "hybrid" if use_hybrid else "dense"
        rerank_cmp_vs_best_so_far = compare_per_question(
            best_so_far["per_question"],
            rerank_eval["per_question"],
            baseline_name=f"{selected_name}+{best_so_far_name}",
            current_name=f"{selected_name}+hybrid_rerank",
        )
        use_rerank = (recall_mean(rerank_eval) or -1) > (recall_mean(best_so_far) or -1)
        rerank_report = {
            "name": "gte_rerank",
            "status": "measured",
            "measured_at": now_iso(),
            "timezone": "Asia/Shanghai",
            "split": "dev",
            "chunking_name": selected_name,
            "after": "hybrid",
            "hybrid_metrics": compact_metrics(hybrid_eval),
            "rerank_metrics": compact_metrics(rerank_eval),
            "comparison_vs_hybrid": rerank_cmp_vs_hybrid,
            "comparison_vs_best_before_rerank": rerank_cmp_vs_best_so_far,
            "adopted": use_rerank,
            "reason": (
                "融合后再 rerank，开发集 Recall@5 高于当时最好链路，纳入主链路。"
                if use_rerank
                else "融合后再 rerank，开发集 Recall@5 未高于当时最好链路，不纳入主链路。不为了必须提升而改参数或挪数字。"
            ),
            "per_question_path": str(rerank_eval_path.relative_to(ROOT)),
        }
        write_json(REPORTS / "rerank_dev.json", rerank_report)

    if use_rerank and rerank_eval is not None:
        locked_retriever = "hybrid_rerank"
        locked_eval = rerank_eval
        locked_eval_path = rerank_eval_path
    elif use_hybrid:
        locked_retriever = "hybrid"
        locked_eval = hybrid_eval
        locked_eval_path = hybrid_eval_path
    else:
        locked_retriever = "dense"
        locked_eval = dense_eval
        locked_eval_path = dense_eval_path

    rerank_settings = False
    if locked_retriever == "hybrid_rerank":
        rerank_settings = (locked_eval.get("settings") or {}).get("rerank") or {
            "requested_model": "gte-rerank",
            "candidates": 20,
        }

    locked = {
        "name": "locked_retrieval",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "chunking_name": selected_name,
        "chunking": selected_chunking["chunking"],
        "retriever": locked_retriever,
        "retrieval": locked_retriever,
        "embedding_model": "text-embedding-v4",
        "embedding_dim": 1024,
        "bm25": locked_retriever in {"hybrid", "hybrid_rerank"},
        "rrf": locked_retriever in {"hybrid", "hybrid_rerank"},
        "rerank": rerank_settings if locked_retriever == "hybrid_rerank" else False,
        "generation": False,
        "dev_Recall@5": locked_eval.get("metrics", {}).get("Recall@5"),
        "dev_Hit@5": locked_eval.get("metrics", {}).get("Hit@5"),
        "selection_reason": (
            f"切分选定 {selected_name}；"
            f"BM25+RRF {'纳入' if use_hybrid else '不纳入'}；"
            f"gte-rerank {'纳入' if use_rerank else '不纳入'}。"
            "全部依据开发集 Recall@5，测试集未参与选型或调参。"
        ),
    }
    write_json(LOCKED_PATH, locked)

    # 4) 用锁定链路的开发集逐题结果落下「上次通过」CI 基线。
    ci_baseline = {
        "name": "ci_recall_baseline",
        "phase": 3,
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "split": "dev",
        "pipeline": locked,
        "n_questions_in_split": locked_eval.get("n_questions_in_split"),
        "n_answerable": locked_eval.get("n_answerable"),
        "n_refusal_excluded": locked_eval.get("n_refusal_excluded"),
        "n_used_for_recall": locked_eval.get("n_used_for_recall"),
        "metrics": locked_eval.get("metrics"),
        "Recall@5": locked_eval.get("metrics", {}).get("Recall@5"),
        "Hit@5": locked_eval.get("metrics", {}).get("Hit@5"),
        "n_failures_hit@5": locked_eval.get("n_failures_hit@5"),
        "failure_ids": locked_eval.get("failure_ids"),
        "by_type": locked_eval.get("by_type"),
        "gate": {
            "metric": "Recall@5",
            "unit": "questions",
            "rule": "与上次通过的开发集逐题 Recall@5 比较：recall 下降的题数减去上升的题数，若净减少大于 3 则失败。不设绝对 90% 门槛，延迟不进硬门禁。测试集不参与 CI 决策。",
            "max_net_decrease_questions": 3,
        },
        "source_eval": str(locked_eval_path.relative_to(ROOT)),
        "per_question": [
            {
                "id": r["id"],
                "type": r.get("type"),
                "recall@5": r["recall@5"],
                "hit@5": r["hit@5"],
                "mrr@5": r.get("mrr@5"),
                "ndcg@5": r.get("ndcg@5"),
                "n_gold_chunks": r.get("n_gold_chunks"),
            }
            for r in locked_eval["per_question"]
        ],
    }
    ci_baseline_path = REPORTS / "ci_baseline.json"
    write_json(ci_baseline_path, ci_baseline)
    # 用基线自检，确认脚本可在本机跑通。
    ci_self = run_script(
        "ci_check_recall.py",
        "--current",
        str(ci_baseline_path),
        "--baseline",
        str(ci_baseline_path),
    )

    # 5) 第一次跑测试集：先核对封存，再只跑锁定链路。
    seal = verify_testset_seal()
    if not seal["ok"]:
        return fail_and_write(
            {
                "ok": False,
                "stage": "testset_seal",
                "error_type": "testset_seal_mismatch",
                "seal": seal,
                "expected_sha256": EXPECTED_SHA256,
            },
            REPORTS / "phase3_summary.json",
        )
    test_eval_path = REPORTS / "test_run1_eval.json"
    test_eval = run_script(
        "eval_retrieval.py",
        "--split",
        "test",
        "--allow-test",
        "--retriever",
        locked_retriever,
        "--mapping",
        str(official),
        out=test_eval_path,
    )
    if not test_eval.get("ok"):
        return fail_and_write(
            {"ok": False, "stage": "test_run1", "error": test_eval, "seal": seal},
            REPORTS / "phase3_summary.json",
        )
    test_report = {
        "name": "test_run1",
        "status": "measured",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "split": "test",
        "pipeline": locked,
        "testset_seal": test_eval.get("testset_seal") or seal,
        "n_test_questions": test_eval.get("n_questions_in_split"),
        "n_answerable": test_eval.get("n_answerable"),
        "n_refusal_excluded_from_recall": test_eval.get("n_refusal_excluded"),
        "n_used_for_recall": test_eval.get("n_used_for_recall"),
        "metric_definitions": test_eval.get("metric_definitions"),
        "metrics": test_eval.get("metrics"),
        "primary_metric": "Recall@5",
        "Recall@5": test_eval.get("metrics", {}).get("Recall@5"),
        "Hit@5": test_eval.get("metrics", {}).get("Hit@5"),
        "by_type": test_eval.get("by_type"),
        "n_failures_hit@5": test_eval.get("n_failures_hit@5"),
        "failure_ids": test_eval.get("failure_ids"),
        "refusal": test_eval.get("refusal"),
        "generation": "not_run",
        "per_question_path": str(test_eval_path.relative_to(ROOT)),
        "note": (
            "第一次跑测试集。只跑已锁定的最终检索链路，未在测试集上做消融或改参数。"
            f"测试集封存 sha256={EXPECTED_SHA256}，实测核对一致。"
            "本段只评检索，未跑生成或拒答判定；拒答题不计入 Recall@5。"
            "数字不参与调参。"
        ),
    }
    write_json(REPORTS / "test_run1.json", test_report)

    summary = {
        "ok": True,
        "name": "phase3",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "chunking_ablation": {
            "selected": selected_name,
            "reason": selected_reason,
            "strategies": [
                {
                    "name": r["name"],
                    "chunking": r["chunking"],
                    "n_chunks": r["n_chunks"],
                    "n_unmapped_spans": r["n_unmapped_spans"],
                    "Recall@5": r["Recall@5"],
                    "Hit@5": r["Hit@5"],
                    "n_failures_hit@5": r["n_failures_hit@5"],
                    "failure_ids": r["failure_ids"],
                    "vs_section_window_400": r.get("vs_section_window_400"),
                }
                for r in ablation_rows
            ],
        },
        "bm25_rrf": {
            "adopted": use_hybrid,
            "Recall@5": hybrid_report["hybrid"]["Recall@5"],
            "dense_Recall@5": hybrid_report["dense"]["Recall@5"],
            "comparison_vs_dense": hybrid_cmp,
            "reason": hybrid_report["reason"],
        },
        "gte_rerank": {
            "status": rerank_report.get("status"),
            "adopted": use_rerank,
            "error_type": rerank_report.get("error_type"),
            "Recall@5": (rerank_report.get("rerank_metrics") or {}).get("Recall@5"),
            "comparison_vs_hybrid": rerank_report.get("comparison_vs_hybrid"),
            "comparison_vs_best_before_rerank": rerank_report.get("comparison_vs_best_before_rerank"),
            "reason": rerank_report.get("reason"),
        },
        "locked_pipeline": locked,
        "ci": {
            "baseline_path": str(ci_baseline_path.relative_to(ROOT)),
            "workflow": ".github/workflows/retrieval-ci.yml",
            "gate": ci_baseline["gate"],
            "self_check": {k: v for k, v in ci_self.items() if k != "comparison"},
            "needs_repo_secret": True,
            "secret_name": "DASHSCOPE_API_KEY",
        },
        "test_run1": compact_metrics(test_eval) | {
            "report_path": "reports/test_run1.json",
            "per_question_path": str(test_eval_path.relative_to(ROOT)),
            "generation": "not_run",
        },
        "testset_seal": seal,
    }
    write_json(REPORTS / "phase3_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
