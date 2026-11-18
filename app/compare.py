"""开发集配对比：错→对 / 对→错 与精确 McNemar 检验。"""

from __future__ import annotations

import math


def mcnemar_exact(n_improved: int, n_regressed: int) -> dict:
    """双向精确 McNemar（分歧对上的二项检验，p=0.5）。"""
    b = int(n_improved)
    c = int(n_regressed)
    n = b + c
    if n == 0:
        return {
            "n_improved": b,
            "n_regressed": c,
            "n_discordant": 0,
            "p_value": 1.0,
            "test": "mcnemar_exact_binomial",
            "significant_at_0.05": False,
            "direction": "tied",
            "interpretation": "无分歧题目，无法检验。",
        }
    k = min(b, c)
    cdf = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    p_value = min(1.0, 2.0 * cdf)
    if b > c:
        direction = "positive"
    elif b < c:
        direction = "negative"
    else:
        direction = "tied"
    significant = p_value <= 0.05
    if direction == "positive" and not significant:
        interpretation = "方向为正但未达显著"
    elif direction == "positive" and significant:
        interpretation = "方向为正且在 0.05 水平显著"
    elif direction == "negative" and not significant:
        interpretation = "方向为负但未达显著"
    elif direction == "negative" and significant:
        interpretation = "方向为负且在 0.05 水平显著"
    else:
        interpretation = "无方向差异"
    return {
        "n_improved": b,
        "n_regressed": c,
        "n_discordant": n,
        "p_value": p_value,
        "test": "mcnemar_exact_binomial",
        "significant_at_0.05": significant,
        "direction": direction,
        "interpretation": interpretation,
    }


def compare_per_question(
    baseline: list[dict],
    current: list[dict],
    *,
    success_field: str = "hit@5",
    recall_field: str = "recall@5",
    baseline_name: str = "baseline",
    current_name: str = "current",
) -> dict:
    """以 Hit@5 作为对/错，同时报告 Recall@5 升降题数。"""
    base_map = {r["id"]: r for r in baseline}
    curr_map = {r["id"]: r for r in current}
    ids = sorted(set(base_map) & set(curr_map))
    only_base = sorted(set(base_map) - set(curr_map))
    only_curr = sorted(set(curr_map) - set(base_map))

    improved_ids = []
    regressed_ids = []
    recall_up_ids = []
    recall_down_ids = []
    recall_same = 0
    for qid in ids:
        b = base_map[qid]
        c = curr_map[qid]
        b_hit = float(b[success_field])
        c_hit = float(c[success_field])
        if b_hit < 0.5 and c_hit >= 0.5:
            improved_ids.append(qid)
        elif b_hit >= 0.5 and c_hit < 0.5:
            regressed_ids.append(qid)
        b_rec = float(b[recall_field])
        c_rec = float(c[recall_field])
        if c_rec > b_rec + 1e-12:
            recall_up_ids.append(qid)
        elif c_rec < b_rec - 1e-12:
            recall_down_ids.append(qid)
        else:
            recall_same += 1

    mcn = mcnemar_exact(len(improved_ids), len(regressed_ids))
    return {
        "baseline": baseline_name,
        "current": current_name,
        "success_definition": "Hit@5==1 视为对，否则为错（top5 至少命中一条相关切片）",
        "n_comparable": len(ids),
        "n_only_baseline": len(only_base),
        "n_only_current": len(only_curr),
        "wrong_to_right": {"n": len(improved_ids), "ids": improved_ids},
        "right_to_wrong": {"n": len(regressed_ids), "ids": regressed_ids},
        "mcnemar": mcn,
        "recall@5_changes": {
            "n_up": len(recall_up_ids),
            "ids_up": recall_up_ids,
            "n_down": len(recall_down_ids),
            "ids_down": recall_down_ids,
            "n_same": recall_same,
            "net_decrease_questions": len(recall_down_ids) - len(recall_up_ids),
        },
    }
