#!/usr/bin/env python3
"""在开发集上跑 B0 基线：默认切分 + 锁定 embedding 的纯密集检索。

不跑测试集，不加 BM25 / RRF / gte-rerank，不跑生成。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
REPORT_PATH = ROOT / "reports" / "b0_dev.json"
SHANGHAI = timezone(timedelta(hours=8))


def run(script: str, *args: str) -> dict:
    cmd = [str(PYTHON), str(ROOT / "scripts" / script), *args]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        # 只回传退出码与 stdout/stderr 文本；调用方不得把密钥写入任何文件。
        err = {
            "ok": False,
            "error_type": "subprocess_failed",
            "script": script,
            "returncode": proc.returncode,
        }
        # 尝试解析子进程 JSON
        for stream in (proc.stdout, proc.stderr):
            try:
                payload = json.loads(stream.strip().splitlines()[-1] if stream.strip() else "")
                if isinstance(payload, dict) and payload.get("error_type"):
                    err["error_type"] = payload["error_type"]
                    err["child"] = payload
                    break
            except Exception:  # noqa: BLE001
                continue
        if "child" not in err:
            err["stdout_tail"] = (proc.stdout or "")[-2000:]
            err["stderr_tail"] = (proc.stderr or "")[-2000:]
        print(json.dumps(err, ensure_ascii=False, indent=2))
        raise SystemExit(proc.returncode or 1)
    return json.loads(proc.stdout)


def main() -> int:
    chunk_report = run("chunk_and_embed.py")
    map_report = run("map_annotations.py")
    eval_path = REPORT_PATH.with_name("b0_dev_eval.json")
    eval_report = run("eval_retrieval.py", "--split", "dev", "--out", str(eval_path))

    # 压缩评测细节：完整 per_question 另存，主报告保留设定与汇总。
    metrics = eval_report.get("metrics", {})
    report = {
        "name": "B0",
        "status": "measured" if eval_report.get("ok") else "failed",
        "measured_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "split": "dev",
        "n_dev_questions": eval_report.get("n_questions_in_split"),
        "n_answerable": eval_report.get("n_answerable"),
        "n_refusal_excluded_from_recall": eval_report.get("n_refusal_excluded"),
        "settings": {
            "embedding_model": "text-embedding-v4",
            "embedding_dim": 1024,
            "retrieval": "dense_only",
            "distance": "pgvector_cosine_<=>",
            "score": "1 - cosine_distance",
            "topk_for_recall": 5,
            "k_retrieved": eval_report.get("k_retrieved"),
            "chunking": chunk_report.get("chunking"),
            "n_chunks": chunk_report.get("n_chunks"),
            "n_embeddings": chunk_report.get("n_embeddings"),
            "bm25": False,
            "rrf": False,
            "rerank": False,
            "generation": False,
        },
        "annotation_mapping": {
            "rule": map_report.get("rule"),
            "threshold": map_report.get("threshold"),
            "n_chunks": map_report.get("n_chunks"),
            "n_gold_spans": map_report.get("n_gold_spans"),
            "n_unique_relevant_chunks": map_report.get("n_unique_relevant_chunks"),
            "n_mapped_question_chunk_pairs": map_report.get("n_mapped_question_chunk_pairs"),
            "n_unmapped_spans": map_report.get("n_unmapped_spans"),
            "dev_answerable": map_report.get("dev_answerable"),
        },
        "metric_definitions": eval_report.get("metric_definitions"),
        "metrics": metrics,
        "primary_metric": "Recall@5",
        "Recall@5": metrics.get("Recall@5"),
        "by_type": eval_report.get("by_type"),
        "n_failures_hit@5": eval_report.get("n_failures_hit@5"),
        "failure_ids": eval_report.get("failure_ids"),
        "refusal": eval_report.get("refusal"),
        "per_question_path": str(eval_path.relative_to(ROOT)),
        "note": "全部指标来自本机对开发集的实测。高基线予以保留，不为后续提升而压低。本阶段不跑测试集、不用测试集调参。",
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if eval_report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
