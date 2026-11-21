#!/usr/bin/env python3
"""第三段编排：三种切分消融、BM25+RRF、gte-rerank、本地回归，以及第一次测试集评测。

选型与调参只用开发集。测试集只在锁定最终检索链路之后跑一次，数字不参与调参。
本脚本在机器人电脑上把数据放在 POLICY_QA_HOME（默认 /tmp/policy-qa-stage3-test），
不写入 C 盘，也不修改用户 Windows 上的 D:/policy-qa。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
REPORTS = ROOT / "reports"
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_HOME = Path("/tmp/policy-qa-stage3-test")

sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from app.chunking import (  # noqa: E402
    ABLATION_STRATEGY_NAMES,
    chunk_key,
    chunk_sections,
    get_strategy,
    params_dict,
)
from app.compare import compare_per_question  # noqa: E402
from app.config import clear_settings_cache, get_settings  # noqa: E402
from app.corpus_io import load_corpus  # noqa: E402
from app.db import init_db  # noqa: E402
from app.ingest import ingest_corpus  # noqa: E402
from app.pipeline import LOCKED_PATH  # noqa: E402
from app.regression import check_regression, is_better_or_equal  # noqa: E402
from eval.seal import verify_testset_seal  # noqa: E402


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


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fail_and_write(report: dict, path: Path) -> int:
    write_json(path, report)
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
        "definitions": eval_report.get("definitions"),
        "refusal": eval_report.get("refusal"),
        "testset_seal": eval_report.get("testset_seal"),
    }


def recall_mean(eval_report: dict) -> float | None:
    value = (eval_report.get("metrics") or {}).get("Recall@5")
    if isinstance(value, dict):
        return value.get("mean")
    return value


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


def ingest_strategy(name: str) -> dict:
    clear_settings_cache()
    settings = get_settings()
    init_db(settings)
    started = datetime.now(SHANGHAI)
    result = ingest_corpus(settings, chunking_name=name)
    result["home"] = str(settings.home_dir())
    result["db_path"] = str(settings.db_path())
    result["measured_at"] = started.isoformat(timespec="seconds")
    return result


def last_passed_payload(name: str, eval_report: dict, *, chunking_name: str, retriever: str) -> dict:
    return {
        "name": name,
        "split": "dev",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "chunking_name": chunking_name,
        "retriever": retriever,
        "metrics": eval_report.get("metrics"),
        "failure_ids": eval_report.get("failure_ids"),
        "per_question": [
            {"id": row["id"], "type": row["type"], "hit@5": row["hit@5"], "recall@5": row["recall@5"]}
            for row in eval_report.get("per_question") or []
        ],
    }


def main() -> int:
    home = Path(os.environ.get("POLICY_QA_HOME") or DEFAULT_HOME)
    os.environ["POLICY_QA_HOME"] = str(home)
    load_dotenv(ROOT / ".env", override=False)
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    clear_settings_cache()
    settings = get_settings()
    REPORTS.mkdir(parents=True, exist_ok=True)

    locations = {
        "note": "安装与评测数据位置如下，未默认写入 C 盘，也未改动用户电脑上的 D:/policy-qa。",
        "policy_qa_home": str(settings.home_dir()),
        "database": str(settings.db_path()),
        "embed_cache_write": str(settings.data_dir() / "embed_cache"),
        "logs": str(settings.log_dir()),
        "repo": str(ROOT),
        "venv_python": str(PYTHON),
    }
    print(json.dumps({"stage": 3, "locations": locations}, ensure_ascii=False, indent=2), flush=True)

    docs = load_corpus()
    chunk_sets: dict[str, set[tuple]] = {}
    ablation_rows = []
    eval_by_name: dict[str, dict] = {}
    ingest_by_name: dict[str, dict] = {}

    for name in ABLATION_STRATEGY_NAMES:
        params = get_strategy(name)
        chunks = chunk_sections(docs, params)
        chunk_sets[name] = {chunk_key(c) for c in chunks}
        ingest_report = ingest_strategy(name)
        ingest_by_name[name] = {
            k: ingest_report.get(k)
            for k in (
                "skipped",
                "reason",
                "n_docs",
                "n_chunks",
                "n_embeddings",
                "embedding_api_calls",
                "embedding_texts_sent",
                "chunking_name",
                "fingerprint",
                "home",
                "db_path",
            )
        }
        map_path = REPORTS / f"mapping_{name}.json"
        map_report = run_script("map_annotations.py", "--out", str(map_path))
        if map_path.is_file():
            full_map = json.loads(map_path.read_text(encoding="utf-8"))
        else:
            full_map = map_report
        if map_report.get("error_type") and not map_path.is_file():
            return fail_and_write(
                {"ok": False, "stage": "map_annotations", "strategy": name, "error": map_report, "locations": locations},
                REPORTS / "phase3_summary.json",
            )
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
                {"ok": False, "stage": "chunking_eval", "strategy": name, "error": eval_report, "locations": locations},
                REPORTS / "phase3_summary.json",
            )
        eval_by_name[name] = eval_report
        ablation_rows.append(
            {
                "name": name,
                "chunking": params_dict(params, name=name),
                "n_chunks": len(chunks),
                "n_unmapped_spans": full_map.get("n_unmapped_spans"),
                "dev_answerable": full_map.get("dev_answerable"),
                "ingest": ingest_by_name[name],
                "metrics": eval_report.get("metrics"),
                "Recall@5": (eval_report.get("metrics") or {}).get("Recall@5"),
                "Hit@5": (eval_report.get("metrics") or {}).get("Hit@5"),
                "n_failures_hit@5": eval_report.get("n_failures_hit@5"),
                "failure_ids": eval_report.get("failure_ids"),
                "per_question_path": str(eval_path.relative_to(ROOT)),
            }
        )

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
            {"ok": False, "error_type": "chunking_strategies_not_distinct", "pairwise": pairwise, "locations": locations},
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

    def selection_key(row: dict) -> tuple:
        mean = row.get("Recall@5")
        if isinstance(mean, dict):
            mean = mean.get("mean")
        if mean is None:
            mean = -1.0
        prefer_b0 = 1 if row["name"] == baseline_name else 0
        return (float(mean), prefer_b0)

    selected_chunking = max(ablation_rows, key=selection_key)
    selected_name = selected_chunking["name"]
    selected_reason = (
        f"按开发集纯密集检索 Recall@5 最高者锁定切分；选定 {selected_name}，"
        f"Recall@5={selected_chunking.get('Recall@5')}。"
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
        "locations": locations,
        "strategies_distinct": distinct,
        "pairwise_chunk_sets": pairwise,
        "strategies": ablation_rows,
        "comparisons_vs_section_window_400": comparisons,
        "selected": selected_name,
        "selection_rule": "开发集 Recall@5 宏平均最高；并列时保留 section_window_400（B0）。测试集数字不参与选型。",
        "selection_reason": selected_reason,
    }
    write_json(REPORTS / "chunking_ablation.json", ablation_report)

    reload = ingest_strategy(selected_name)
    official_map = run_script("map_annotations.py")
    if official_map.get("error_type") and not official_map.get("ok"):
        return fail_and_write(
            {"ok": False, "stage": "official_map", "error": official_map, "locations": locations},
            REPORTS / "phase3_summary.json",
        )
    official = ROOT / "eval" / "annotation_mapping.json"
    dense_eval = eval_by_name[selected_name]
    dense_eval_path = REPORTS / f"chunking_{selected_name}_dev_eval.json"

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
            {"ok": False, "stage": "hybrid_eval", "error": hybrid_eval, "locations": locations},
            REPORTS / "phase3_summary.json",
        )
    hybrid_cmp = compare_per_question(
        dense_eval["per_question"],
        hybrid_eval["per_question"],
        baseline_name=f"{selected_name}+dense",
        current_name=f"{selected_name}+hybrid",
    )
    use_hybrid = (recall_mean(hybrid_eval) or -1) > (recall_mean(dense_eval) or -1)
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
    if not rerank_eval.get("ok"):
        rerank_report = {
            "name": "gte_rerank",
            "status": "failed",
            "measured_at": now_iso(),
            "timezone": "Asia/Shanghai",
            "split": "dev",
            "chunking_name": selected_name,
            "error_type": rerank_eval.get("error_type") or (rerank_eval.get("child") or {}).get("error_type"),
            "error": {k: v for k, v in rerank_eval.items() if k != "per_question"},
            "adopted": False,
            "reason": "gte-rerank API 调用失败，不编造结果，本步不纳入主链路。",
        }
        write_json(REPORTS / "rerank_dev.json", rerank_report)
        rerank_eval = None
        use_rerank = False
    else:
        rerank_cmp_vs_hybrid = compare_per_question(
            hybrid_eval["per_question"],
            rerank_eval["per_question"],
            baseline_name=f"{selected_name}+hybrid",
            current_name=f"{selected_name}+hybrid_rerank",
        )
        best_so_far = hybrid_eval if use_hybrid else dense_eval
        best_so_far_name = "hybrid" if use_hybrid else "dense"
        rerank_cmp_vs_best = compare_per_question(
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
            "comparison_vs_best_before_rerank": rerank_cmp_vs_best,
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
        "dev_Recall@5": recall_mean(locked_eval),
        "dev_Hit@5": (locked_eval.get("metrics") or {}).get("Hit@5"),
        "dev_metrics": locked_eval.get("metrics"),
        "failure_ids": locked_eval.get("failure_ids"),
        "selection_reason": (
            f"切分选定 {selected_name}；"
            f"BM25+RRF {'纳入' if use_hybrid else '不纳入'}；"
            f"gte-rerank {'纳入' if use_rerank else '不纳入'}。"
            "全部依据开发集 Recall@5，测试集未参与选型或调参。"
        ),
        "eval_report": str(locked_eval_path.relative_to(ROOT)),
    }
    write_json(LOCKED_PATH, locked)

    baseline_path = REPORTS / "last_passed_dev_recall.json"
    if not baseline_path.is_file():
        return fail_and_write(
            {"ok": False, "stage": "regression", "error_type": "missing_last_passed", "locations": locations},
            REPORTS / "phase3_summary.json",
        )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    regression = check_regression(baseline, locked_eval, baseline_path=baseline_path)
    write_json(REPORTS / "stage3_regression.json", regression)
    updated_last_passed = False
    if regression.get("passed") and is_better_or_equal(baseline, locked_eval):
        write_json(
            baseline_path,
            last_passed_payload("stage3_locked", locked_eval, chunking_name=selected_name, retriever=locked_retriever),
        )
        updated_last_passed = True

    # 第一次跑测试集：先校验封存哈希，数字不参与调参。
    seal = verify_testset_seal()
    if not seal.get("ok"):
        return fail_and_write(
            {
                "ok": False,
                "stage": "testset_seal",
                "error_type": "testset_seal_mismatch",
                "seal": seal,
                "locations": locations,
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
            {"ok": False, "stage": "test_run1", "error": test_eval, "seal": seal, "locations": locations},
            REPORTS / "phase3_summary.json",
        )
    test_report = {
        "name": "test_run1",
        "status": "measured",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "split": "test",
        "note": "轻量版第一次测试集评测。数字不参与调参，也不回写到切分或检索选型。",
        "chunking_name": selected_name,
        "retriever": locked_retriever,
        "testset_seal": test_eval.get("testset_seal") or seal,
        "metrics": test_eval.get("metrics"),
        "by_type": test_eval.get("by_type"),
        "n_answerable": test_eval.get("n_answerable"),
        "n_failures_hit@5": test_eval.get("n_failures_hit@5"),
        "failure_ids": test_eval.get("failure_ids"),
        "refusal": test_eval.get("refusal"),
        "eval_report": str(test_eval_path.relative_to(ROOT)),
        "generation": "not_run",
    }
    write_json(REPORTS / "test_run1.json", test_report)

    summary = {
        "ok": bool(regression.get("passed")),
        "name": "stage3_summary",
        "measured_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "locations": locations,
        "reload_selected_chunking": {
            "skipped": reload.get("skipped"),
            "reason": reload.get("reason"),
            "n_chunks": reload.get("n_chunks"),
            "embedding_api_calls": reload.get("embedding_api_calls"),
            "embedding_texts_sent": reload.get("embedding_texts_sent"),
        },
        "chunking_ablation": {
            "selected": selected_name,
            "selection_reason": selected_reason,
            "strategies": [
                {
                    "name": r["name"],
                    "n_chunks": r["n_chunks"],
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
            "adopted": use_rerank if rerank_eval is not None else False,
            "error_type": rerank_report.get("error_type"),
            "Recall@5": (rerank_report.get("rerank_metrics") or {}).get("Recall@5"),
            "comparison_vs_hybrid": rerank_report.get("comparison_vs_hybrid"),
            "comparison_vs_best_before_rerank": rerank_report.get("comparison_vs_best_before_rerank"),
            "reason": rerank_report.get("reason"),
        },
        "locked_pipeline": locked,
        "regression": regression,
        "updated_last_passed": updated_last_passed,
        "test_run1": compact_metrics(test_eval)
        | {
            "report_path": "reports/test_run1.json",
            "per_question_path": str(test_eval_path.relative_to(ROOT)),
            "generation": "not_run",
        },
        "testset_seal": seal,
    }
    write_json(REPORTS / "phase3_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not regression.get("passed"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
