"""开发集回归：与上次通过的结果比，Hit@5 或 Recall@5 净减少超过 1 题则失败。

不设绝对 90% 门槛，延迟不进硬门禁，测试集不参与决策。
可在本机对已有评测报告运行，无需再次调用 API。
"""

from __future__ import annotations

from pathlib import Path

from app.compare import compare_per_question

DEFAULT_MAX_NET_LOSS = 1


def _per_question(payload: dict) -> list[dict]:
    if payload.get("per_question"):
        return payload["per_question"]
    raise ValueError("报告缺少 per_question，无法做逐题回归比较")


def _split(payload: dict) -> str | None:
    return payload.get("split")


def _recall_value(payload: dict) -> float | None:
    metrics = payload.get("metrics") or {}
    value = metrics.get("Recall@5", payload.get("Recall@5"))
    if isinstance(value, dict):
        return value.get("mean")
    return value


def _hit_value(payload: dict) -> float | None:
    metrics = payload.get("metrics") or {}
    value = metrics.get("Hit@5", payload.get("Hit@5"))
    if isinstance(value, dict):
        return value.get("mean")
    return value


def check_regression(
    baseline: dict,
    current: dict,
    *,
    max_net_loss: int = DEFAULT_MAX_NET_LOSS,
    baseline_path: str | Path | None = None,
) -> dict:
    """对照上次通过的开发集结果。净减少 = 变差题数 - 变好题数。"""
    if _split(baseline) and _split(baseline) != "dev":
        return {
            "ok": False,
            "passed": False,
            "checked": True,
            "error_type": "baseline_not_dev",
            "split": _split(baseline),
        }
    if _split(current) and _split(current) != "dev":
        return {
            "ok": False,
            "passed": False,
            "checked": True,
            "error_type": "current_not_dev",
            "split": _split(current),
        }
    try:
        base_q = _per_question(baseline)
        curr_q = _per_question(current)
    except ValueError as exc:
        return {
            "ok": False,
            "passed": False,
            "checked": True,
            "error_type": "missing_per_question",
            "detail": str(exc),
        }
    comparison = compare_per_question(
        base_q,
        curr_q,
        baseline_name=str(baseline.get("name") or "last_passed"),
        current_name=str(current.get("retriever") or current.get("name") or "current"),
    )
    if comparison["n_only_baseline"] or comparison["n_only_current"] or comparison["n_comparable"] == 0:
        return {
            "ok": False,
            "passed": False,
            "checked": True,
            "error_type": "question_set_mismatch",
            "comparison": comparison,
        }
    hit_net_loss = comparison["right_to_wrong"]["n"] - comparison["wrong_to_right"]["n"]
    recall_net_loss = comparison["recall@5_changes"]["net_decrease_questions"]
    passed = hit_net_loss <= max_net_loss and recall_net_loss <= max_net_loss
    return {
        "ok": True,
        "passed": passed,
        "checked": True,
        "baseline_path": str(baseline_path) if baseline_path else None,
        "baseline_name": baseline.get("name") or baseline.get("split"),
        "gate": {
            "metrics": ["Hit@5", "Recall@5"],
            "unit": "questions",
            "rule": (
                "与上次通过的开发集逐题结果比较：Hit@5 净减少（由对变错减去由错变对）"
                f"或 Recall@5 净减少（下降题数减去上升题数）超过 {max_net_loss} 题则失败。"
                "不设绝对 90% 门槛，延迟不进硬门禁。测试集不参与回归决策。"
            ),
            "max_net_loss": max_net_loss,
        },
        "baseline_Recall@5": _recall_value(baseline),
        "current_Recall@5": _recall_value(current),
        "baseline_Hit@5": _hit_value(baseline),
        "current_Hit@5": _hit_value(current),
        "n_common": comparison["n_comparable"],
        "became_wrong": comparison["right_to_wrong"]["ids"],
        "became_right": comparison["wrong_to_right"]["ids"],
        "hit@5_net_loss": hit_net_loss,
        "recall@5_n_up": comparison["recall@5_changes"]["n_up"],
        "recall@5_n_down": comparison["recall@5_changes"]["n_down"],
        "recall@5_net_loss": recall_net_loss,
        "comparison": comparison,
        "message": (
            "通过：开发集 Hit@5 净减少 "
            f"{hit_net_loss} 题、Recall@5 净减少 {recall_net_loss} 题，均未超过 {max_net_loss} 题门禁。"
            if passed
            else (
                "失败：开发集 Hit@5 净减少 "
                f"{hit_net_loss} 题，或 Recall@5 净减少 {recall_net_loss} 题，超过 {max_net_loss} 题门禁。"
            )
        ),
    }


def is_better_or_equal(baseline: dict, current: dict) -> bool:
    """最终链路优于或持平 B0：Recall@5 与 Hit@5 均不低于基线。"""
    base_rec = _recall_value(baseline)
    curr_rec = _recall_value(current)
    base_hit = _hit_value(baseline)
    curr_hit = _hit_value(current)
    if base_rec is None or curr_rec is None or base_hit is None or curr_hit is None:
        return False
    return curr_rec >= base_rec - 1e-12 and curr_hit >= base_hit - 1e-12
