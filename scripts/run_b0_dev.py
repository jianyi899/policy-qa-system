#!/usr/bin/env python3
"""在开发集上跑 B0 基线：默认切分 section_window_400 + 锁定 embedding 的纯密集检索。

不跑测试集，不加 BM25 / RRF / gte-rerank，不跑生成。
须先在目标 POLICY_QA_HOME 完成入库（python -m app.start --ingest-only）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
REPORT_PATH = ROOT / "reports" / "b0_dev.json"
EVAL_PATH = ROOT / "reports" / "b0_dev_eval.json"
BASELINE_PATH = ROOT / "reports" / "last_passed_dev_recall.json"
SHANGHAI = timezone(timedelta(hours=8))


def run(script: str, *args: str) -> dict:
    cmd = [str(PYTHON), str(ROOT / "scripts" / script), *args]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        err = {
            "ok": False,
            "error_type": "subprocess_failed",
            "script": script,
            "returncode": proc.returncode,
            "command": cmd,
        }
        for stream in (proc.stdout, proc.stderr):
            raw = (stream or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw if raw.startswith("{") else raw.splitlines()[-1])
                if isinstance(payload, dict) and (payload.get("error_type") or payload.get("ok") is False):
                    err["error_type"] = payload.get("error_type") or err["error_type"]
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
    map_report = run("map_annotations.py")
    eval_report = run("eval_retrieval.py", "--split", "dev", "--out", str(EVAL_PATH))
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
        "command": [
            str(PYTHON),
            "scripts/run_b0_dev.py",
        ],
        "steps": [
            [str(PYTHON), "scripts/map_annotations.py"],
            [str(PYTHON), "scripts/eval_retrieval.py", "--split", "dev", "--out", str(EVAL_PATH)],
        ],
        "settings": {
            "embedding_model": eval_report.get("settings", {}).get("embedding_model"),
            "embedding_dim": eval_report.get("settings", {}).get("embedding_dim"),
            "retrieval": "dense_only",
            "distance": eval_report.get("settings", {}).get("distance"),
            "score": eval_report.get("settings", {}).get("score"),
            "topk_for_recall": 5,
            "k_retrieved": eval_report.get("k_retrieved"),
            "chunking_name": "section_window_400",
            "n_chunks": eval_report.get("settings", {}).get("n_chunks"),
            "bm25": False,
            "rrf": False,
            "rerank": False,
            "generation": False,
        },
        "mapping": {
            "ok": map_report.get("ok"),
            "n_chunks": map_report.get("n_chunks"),
            "n_unmapped_spans": map_report.get("n_unmapped_spans"),
            "dev_answerable": map_report.get("dev_answerable"),
        },
        "metrics": metrics,
        "by_type": eval_report.get("by_type"),
        "n_failures_hit@5": eval_report.get("n_failures_hit@5"),
        "failure_ids": eval_report.get("failure_ids"),
        "refusal": eval_report.get("refusal"),
        "regression": eval_report.get("regression"),
        "eval_report": str(EVAL_PATH),
        "note": "数字来自本机实测。测试集本段未跑，结果不得用于调参。高基线予以保留，不刻意压低。",
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if eval_report.get("ok"):
        # 作为后续回归对照的“上次通过”结果。完整 per_question 一并保存。
        baseline = {
            "name": "B0",
            "split": "dev",
            "measured_at": report["measured_at"],
            "timezone": "Asia/Shanghai",
            "metrics": metrics,
            "failure_ids": eval_report.get("failure_ids"),
            "per_question": [
                {"id": row["id"], "type": row["type"], "hit@5": row["hit@5"], "recall@5": row["recall@5"]}
                for row in eval_report.get("per_question") or []
            ],
        }
        BASELINE_PATH.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["wrote_baseline"] = str(BASELINE_PATH)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if eval_report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
