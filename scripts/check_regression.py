#!/usr/bin/env python3
"""对照上次通过的开发集结果做回归，不必再次调用模型接口。

与 reports/last_passed_dev_recall.json 比较 Hit@5 与 Recall@5：
净减少（变差题数减去变好题数）超过 1 题则失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.regression import check_regression  # noqa: E402

DEFAULT_BASELINE = ROOT / "reports" / "last_passed_dev_recall.json"


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check_reports(baseline: Path | dict, current: Path | dict, *, max_net_loss: int = 1) -> dict:
    baseline_path = None
    if isinstance(baseline, Path):
        baseline_path = baseline
        if not baseline.is_file():
            return {
                "ok": False,
                "passed": False,
                "checked": False,
                "error_type": "missing_baseline",
                "path": str(baseline),
            }
        baseline = load_report(baseline)
    if isinstance(current, Path):
        if not current.is_file():
            return {
                "ok": False,
                "passed": False,
                "checked": False,
                "error_type": "missing_current",
                "path": str(current),
            }
        current = load_report(current)
    return check_regression(baseline, current, max_net_loss=max_net_loss, baseline_path=baseline_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--max-net-loss", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    report = check_reports(args.baseline, args.current, max_net_loss=args.max_net_loss)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not report.get("ok"):
        return 2
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
