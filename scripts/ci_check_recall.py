#!/usr/bin/env python3
"""开发集 Recall@5 门禁：与上次通过的逐题结果比，净减少超过 3 题则失败。

不设绝对 90% 门槛，延迟不进硬门禁，测试集不参与决策。
可在本机对已有评测报告运行，无需再次调用 API。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compare import compare_per_question  # noqa: E402

DEFAULT_BASELINE = ROOT / "reports" / "ci_baseline.json"
MAX_NET_DECREASE = 3


def _per_question(payload: dict) -> list[dict]:
    if "per_question" in payload and payload["per_question"]:
        return payload["per_question"]
    raise ValueError("报告缺少 per_question，无法做逐题门禁比较")


def _split(payload: dict) -> str | None:
    return payload.get("split")


def check(baseline: dict, current: dict, *, max_net_decrease: int = MAX_NET_DECREASE) -> dict:
    if _split(baseline) and _split(baseline) != "dev":
        return {"ok": False, "error_type": "baseline_not_dev", "split": _split(baseline)}
    if _split(current) and _split(current) != "dev":
        return {"ok": False, "error_type": "current_not_dev", "split": _split(current)}
    base_q = _per_question(baseline)
    curr_q = _per_question(current)
    comparison = compare_per_question(
        base_q,
        curr_q,
        baseline_name="ci_baseline",
        current_name="current",
    )
    if comparison["n_only_baseline"] or comparison["n_only_current"] or comparison["n_comparable"] == 0:
        return {
            "ok": False,
            "passed": False,
            "error_type": "question_set_mismatch",
            "comparison": comparison,
        }
    net = comparison["recall@5_changes"]["net_decrease_questions"]
    passed = net <= max_net_decrease
    base_recall = (baseline.get("Recall@5") or baseline.get("metrics", {}).get("Recall@5") or {})
    curr_recall = (current.get("Recall@5") or current.get("metrics", {}).get("Recall@5") or {})
    return {
        "ok": True,
        "passed": passed,
        "gate": {
            "metric": "Recall@5",
            "unit": "questions",
            "rule": (
                "与上次通过的开发集逐题 Recall@5 比较：recall 下降的题数减去上升的题数，"
                f"若净减少大于 {max_net_decrease} 则失败。不设绝对 90% 门槛，延迟不进硬门禁。"
                "测试集不参与 CI 决策。"
            ),
            "max_net_decrease_questions": max_net_decrease,
        },
        "baseline_Recall@5": base_recall,
        "current_Recall@5": curr_recall,
        "n_recall_up": comparison["recall@5_changes"]["n_up"],
        "n_recall_down": comparison["recall@5_changes"]["n_down"],
        "net_decrease_questions": net,
        "hit@5_wrong_to_right": comparison["wrong_to_right"],
        "hit@5_right_to_wrong": comparison["right_to_wrong"],
        "comparison": comparison,
        "message": (
            "通过：开发集 Recall@5 净减少 "
            f"{net} 题，未超过 {max_net_decrease} 题门禁。"
            if passed
            else (
                "失败：开发集 Recall@5 净减少 "
                f"{net} 题，超过 {max_net_decrease} 题门禁。"
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--max-net-decrease", type=int, default=MAX_NET_DECREASE)
    args = parser.parse_args()
    if not args.baseline.is_file():
        print(json.dumps({"ok": False, "error_type": "missing_baseline", "path": str(args.baseline)}, ensure_ascii=False, indent=2))
        return 2
    if not args.current.is_file():
        print(json.dumps({"ok": False, "error_type": "missing_current", "path": str(args.current)}, ensure_ascii=False, indent=2))
        return 2
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    current = json.loads(args.current.read_text(encoding="utf-8"))
    report = check(baseline, current, max_net_decrease=args.max_net_decrease)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("ok"):
        return 2
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
