"""开发集 / 展示题的要点核对。不做模型代打分，不调用 DeepSeek。"""

from __future__ import annotations

import re

REFUSAL_CUES = (
    "无法回答",
    "不能回答",
    "无法依据",
    "无法根据",
    "超出",
    "未覆盖",
    "没有覆盖",
    "未收录",
    "没有相应",
    "没有相关",
    "未查到相关",
    "查不到相关",
    "不属于",
    "不在本系统",
    "超出本",
    "转人工",
    "转交人工",
    "请联系人工",
    "请转",
    "建议转",
    "语料未",
    "政策库中没有",
    "现有政策无法",
    "现有条款无法",
    "不便作答",
    "无法从现有",
    "需咨询",
    "请咨询",
    "不属于售后政策",
    "超出政策问答",
    "没有找到相关政策",
    "没有可供引用",
    "未包含",
    "不在本助手",
)


def contains_any(text: str, needles: tuple[str, ...] | list[str]) -> bool:
    return any(s in (text or "") for s in needles)


def is_refusal_answer(answer: str) -> bool:
    text = answer or ""
    return contains_any(text, REFUSAL_CUES)


def check_q041_answer(answer: str) -> dict:
    """Q041：激活后的手机数码须按品类专项作答，并说明专类依据。"""
    text = answer or ""
    no_reason_denied = contains_any(
        text,
        (
            "不再支持七天无理由",
            "不适用通用七天无理由",
            "不适用七天无理由",
            "不能按七天无理由",
            "不能按无理由",
            "不能走七天无理由",
            "无法按七天无理由",
            "不支持七天无理由",
            "不再支持无理由",
            "不能再按七天无理由",
            "不可按七天无理由",
            "不可以按七天无理由",
            "不能适用七天无理由",
            "激活后不适用",
            "激活后不支持七天",
            "激活后不再支持",
        ),
    )
    wrongly_allows = contains_any(
        text,
        (
            "仍可按七天无理由",
            "仍可七天无理由",
            "还能按七天无理由",
            "可以按七天无理由",
            "可按七天无理由退",
            "激活后仍可无理由",
            "激活后可以七天无理由",
        ),
    )
    quality_only = ("质量问题" in text) and contains_any(
        text,
        ("仅", "只", "只能", "仅能", "仅支持", "只支持", "仅就"),
    )
    fifteen_days = contains_any(text, ("15 日", "15日", "十五日", "15 天", "15天", "十五天"))
    category_basis = contains_any(
        text,
        (
            "品类",
            "专项",
            "专类",
            "手机数码",
            "P15",
            "优先于",
            "覆盖",
            "更细",
            "更具体",
            "不同于通用",
            "不以通用",
            "不得用通用",
            "不能用通用",
            "替代或缩减",
        ),
    )
    points = [
        {
            "id": "activated_no_reason_denied",
            "expected": "手机数码商品激活后不适用通用七天无理由退货",
            "ok": bool(no_reason_denied and not wrongly_allows),
        },
        {
            "id": "quality_only_after_activation",
            "expected": "激活后仅支持质量问题售后",
            "ok": bool(quality_only),
        },
        {
            "id": "quality_window_15_days",
            "expected": "质量问题应在签收之日起 15 日内申请退货或换货",
            "ok": bool(fifteen_days),
        },
        {
            "id": "category_rule_explained",
            "expected": "说明按品类专项规定作答，以及专类覆盖/优先于通用规则",
            "ok": bool(category_basis),
        },
    ]
    return {
        "question_id": "Q041",
        "meets_expected": all(p["ok"] for p in points),
        "required_points": points,
    }


def check_q042_answer(answer: str) -> dict:
    """Q042（测试集）：SKU 质保 24 个月覆盖通用 12 个月。数字只用于报告，不调参。"""
    text = answer or ""
    has_24 = contains_any(text, ("24 个月", "24个月", "二十四个月"))
    has_12 = contains_any(text, ("12 个月", "12个月", "十二个月"))
    sku_priority = contains_any(
        text,
        (
            "SKU",
            "sku",
            "专项",
            "覆盖",
            "优先",
            "取代",
            "更细",
            "更具体",
            "P08",
            "S3",
        ),
    )
    only_12 = has_12 and not has_24
    points = [
        {
            "id": "warranty_24_months",
            "expected": "SKU-HX-WATCH-S3 的质量保证期限为自签收之日起 24 个月",
            "ok": bool(has_24),
        },
        {
            "id": "sku_covers_general_12",
            "expected": "按冲突排序应优先适用 SKU 级规定，24 个月覆盖通用 12 个月",
            "ok": bool(has_24 and sku_priority and has_12),
        },
        {
            "id": "not_only_general_12",
            "expected": "不得只回答通用 12 个月",
            "ok": not only_12,
        },
    ]
    return {
        "question_id": "Q042",
        "meets_expected": all(p["ok"] for p in points),
        "required_points": points,
    }


def check_refusal(answer: str, *, should_refuse: bool) -> dict:
    refused = is_refusal_answer(answer)
    return {
        "should_refuse": should_refuse,
        "answer_looks_like_refusal": refused,
        "ok": (refused == should_refuse) if should_refuse else (not refused),
        # 可答题若被误拒，记为错误；拒答题必须明确拒答。
    }


def point_coverage(answer: str, points: list[str]) -> dict:
    """粗口径：答案要点中长度>=4 的连续片段，或分词后的关键词是否出现。仅作辅助，不替代专项核对。"""
    text = answer or ""
    items = []
    for point in points or []:
        compact = re.sub(r"\s+", "", point)
        # 取要点里较长的数字/期限片段
        hits = []
        for frag in re.findall(r"[0-9]+(?:\s*[日天个月年])+", point):
            if re.sub(r"\s+", "", frag) in compact and (frag in text or frag.replace(" ", "") in text):
                hits.append(frag)
        approx = False
        if len(compact) >= 8 and (compact[:8] in re.sub(r"\s+", "", text) or compact[-8:] in re.sub(r"\s+", "", text)):
            approx = True
        items.append({"point": point, "keyword_hits": hits, "approx_span": approx})
    return {"items": items}
