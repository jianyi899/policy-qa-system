"""开发集质量与成本计算。用于级联标定，不做模型代打分。"""

from __future__ import annotations

import re

import jieba

from app.answer_check import check_refusal, is_refusal_answer

# 官方刊例价（华北 2 北京，实时推理，非思考模式）。
# 来源：https://help.aliyun.com/zh/model-studio/model-pricing （2026-11 查阅）
# qwen-plus 按输入 ≤128K 档；本系统单次输入远低于该档。
# 单位：元 / 百万 token。
MODEL_PRICES_CNY_PER_MILLION = {
    "qwen3.8-flash": {"input": 0.8, "output": 2.7},
    "qwen-plus": {"input": 0.8, "output": 2.0},
}
PRICE_SOURCE = "https://help.aliyun.com/zh/model-studio/model-pricing"
PRICE_REGION = "华北2（北京）"
PRICE_MODE = "非思考模式，实时推理，qwen-plus 按输入≤128K 档"

CN_NUM = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十五": 15,
    "二十": 20,
    "二十四": 24,
    "三十": 30,
}

_STOP = {
    "的", "了", "是", "在", "和", "或", "与", "及", "等", "并", "而", "被", "把",
    "对", "从", "为", "以", "及其", "以及", "如果", "若", "则", "应", "应当",
    "可以", "可", "仍", "仍可", "需", "需要", "将", "由", "按", "根据", "依据",
}


def cost_cny(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = MODEL_PRICES_CNY_PER_MILLION.get(model)
    if not price:
        return None
    inp = int(input_tokens or 0)
    out = int(output_tokens or 0)
    return inp / 1_000_000 * price["input"] + out / 1_000_000 * price["output"]


def _cn_to_int(token: str) -> int | None:
    if token in CN_NUM:
        return CN_NUM[token]
    if token.isdigit():
        return int(token)
    return None


def extract_fact_numbers(text: str) -> set[tuple[int, str]]:
    """抽出带单位的数字事实，如 (7, 天)、(15, 日)、(12, 月)、(24, 月)。"""
    s = text or ""
    s = s.replace(" ", "")
    out: set[tuple[int, str]] = set()
    pattern = re.compile(
        r"([0-9]+|[一二两三四五六七八九十]{1,3})\s*(个)?(日历)?(天|日|个月|月)"
    )
    for match in pattern.finditer(s):
        n = _cn_to_int(match.group(1))
        if n is None:
            continue
        unit = match.group(4)
        if unit in {"天", "日"}:
            unit = "天"
        elif unit in {"月", "个月"}:
            unit = "月"
        out.add((n, unit))
    return out


def _tokens(text: str) -> set[str]:
    return {t.strip() for t in jieba.cut(text or "") if t.strip() and t.strip() not in _STOP and len(t.strip()) > 1}


def point_covered(answer: str, point: str) -> bool:
    """要点覆盖：金标中的期限数字须在回答中出现（天/日、月视为同单位），且实质词有一定交集。"""
    ans = answer or ""
    gold_nums = extract_fact_numbers(point)
    ans_nums = extract_fact_numbers(ans)
    if gold_nums and not gold_nums.issubset(ans_nums):
        return False
    gold_toks = _tokens(point)
    ans_toks = _tokens(ans)
    if not gold_toks:
        return bool(ans.strip())
    overlap = len(gold_toks & ans_toks)
    return overlap >= min(2, len(gold_toks)) or (overlap / max(1, len(gold_toks)) >= 0.4)


def point_coverage(answer: str, points: list[str] | None) -> dict:
    items = list(points or [])
    if not items:
        return {"n": 0, "n_hit": 0, "recall": None, "hits": []}
    hits = [point_covered(answer, p) for p in items]
    n_hit = sum(1 for x in hits if x)
    return {"n": len(items), "n_hit": n_hit, "recall": n_hit / len(items), "hits": hits}


def score_task(
    *,
    answer: str,
    should_refuse: bool,
    answer_points: list[str] | None,
    citation_score: dict | None,
) -> dict:
    refusal = check_refusal(answer, should_refuse=should_refuse)
    looks_refusal = is_refusal_answer(answer)
    coverage = point_coverage(answer, answer_points) if not should_refuse else {"n": 0, "n_hit": 0, "recall": None, "hits": []}
    cite = citation_score or {}
    cite_recall = cite.get("recall")
    cite_prec = cite.get("precision")
    if should_refuse:
        task_ok = bool(refusal.get("ok"))
    else:
        # 可答题：不得拒答；至少引用到一条金标句，或覆盖至少一条金标要点。
        hit_cite = (cite_recall is not None and float(cite_recall) > 0)
        hit_point = (coverage.get("n_hit") or 0) > 0
        task_ok = (not looks_refusal) and (hit_cite or hit_point)
    return {
        "task_ok": task_ok,
        "refusal": refusal,
        "looks_like_refusal": looks_refusal,
        "point_coverage": coverage,
        "citation_recall": cite_recall,
        "citation_precision": cite_prec,
    }
