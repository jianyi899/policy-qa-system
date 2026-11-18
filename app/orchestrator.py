"""第四段单轮主路径：必须先真实查订单，注入冲突解析，再按锁定检索链路作答。

不做多轮对话、不做句级引用模块、不接入 Langfuse、不升级到 qwen-plus。
"""

from __future__ import annotations

from app.clock import iso, now_shanghai
from app.conflict import resolve
from app.context import SessionContext
from app.generation import GenerationError, generate_answer
from app.search import retrieve
from app.tools import invoke
from app.tools.errors import ToolError

# 主路径口语题，供脚本与验收使用。
FRIDGE_QUESTION = "这单冰箱买了 20 天质量问题还能退吗"


def keyword_for_question(question: str) -> str | None:
    if "冰箱" in question:
        return "冰箱"
    return None


def _compact_chunk(item: dict, *, include_text: bool = False) -> dict:
    keys = [
        "doc_id",
        "section_path",
        "char_start",
        "char_end",
        "title",
        "source_type",
        "source_label",
        "scope_type",
        "scope_value",
        "scope_match",
        "scope_label",
        "effective_date",
        "applicability",
        "conflict_rank",
        "retrieval_rank",
        "score",
    ]
    out = {k: item.get(k) for k in keys if k in item}
    if include_text:
        out["text"] = item.get("text")
    return out


def check_fridge_answer(answer: str, facts: dict) -> dict:
    """对照语料口径检查冰箱主路径回答，不把测试集题目拿来调参。"""
    text = answer or ""
    expired_no_reason = any(
        s in text
        for s in (
            "不能按七天无理由",
            "不能按无理由",
            "不能再依据七天无理由",
            "不得再依据七天无理由",
            "不能走七天无理由",
            "不能依据七天无理由",
            "无法按七天无理由",
            "已超过 7 天",
            "已超过七天",
            "已超 7 天",
            "已超七天",
            "超过 7 天",
            "超过七天",
            "超过7天",
            "已过 7 天",
            "已过七天",
            "超出 7 天无理由",
            "不在七天无理由",
            "不在 7 天无理由",
            "七天无理由时限已",
            "七天无理由已过",
            "无理由时限已过",
            "无理由窗口已",
        )
    )
    quality_still_ok = ("质量问题" in text) and any(
        s in text
        for s in (
            "仍可",
            "还可以",
            "可以申请",
            "可申请",
            "可以退",
            "可退",
            "支持退",
            "退货退款",
            "退换",
            "换货或维修",
            "退货、换货",
            "退、换或修",
            "可以办理",
            "可办理",
        )
    )
    warranty_mentioned = any(
        s in text
        for s in ("12 个月", "12个月", "十二个月", "质保期", "质量保证期限", "质保期限内")
    )
    wrongly_allows_no_reason = any(
        s in text
        for s in (
            "仍可七天无理由",
            "还能七天无理由",
            "可以七天无理由",
            "可按七天无理由",
            "能按七天无理由退",
            "可以按无理由退",
        )
    )
    # 语料没有冰箱 SKU / 大家电专项；若编造 24 个月或 15 日品类时限则不合格。
    invents_special = any(
        s in text
        for s in ("24 个月", "24个月", "冰箱专项", "大家电专项", "签收后 15 日", "签收后15日")
    )
    points = [
        {
            "id": "no_reason_expired",
            "expected": "已超过 7 天无理由窗口，不能按七天无理由退货",
            "ok": bool(expired_no_reason and not wrongly_allows_no_reason),
        },
        {
            "id": "quality_still_refundable",
            "expected": "质量问题在适用质保期内仍可申请退换或维修",
            "ok": bool(quality_still_ok),
        },
        {
            "id": "general_warranty_12m",
            "expected": "本单无 SKU/品类专项质保，适用通用 12 个月质保（或明确在质保期内）",
            "ok": bool(warranty_mentioned),
        },
        {
            "id": "no_invented_special_rule",
            "expected": "不编造冰箱专项或与本单无关的 24 个月 / 15 日规则",
            "ok": not invents_special,
        },
    ]
    optional = [
        {
            "id": "member_does_not_extend",
            "expected": "会员身份不延长七天无理由时限（可选提及）",
            "ok": ("会员" in text and any(s in text for s in ("不延长", "不改变", "不能延长"))),
        },
        {
            "id": "quality_freight_merchant",
            "expected": "质量问题退货运费由商家承担（可选提及）",
            "ok": (("运费" in text) and ("商家" in text or "卖家" in text)),
        },
    ]
    required_ok = all(p["ok"] for p in points)
    return {
        "meets_expected": required_ok,
        "required_points": points,
        "optional_points": optional,
        "order_days_since_receipt": facts.get("days_since_receipt"),
        "order_within_7_day_no_reason": facts.get("within_7_day_no_reason"),
        "order_within_general_warranty_12m": facts.get("within_general_warranty_12m"),
    }


def answer_after_sales(
    question: str,
    ctx: SessionContext,
    *,
    keyword: str | None = None,
    order_no: str | None = None,
    top_k: int = 40,
    max_evidence: int = 12,
) -> dict:
    tool_calls: list[dict] = []
    kw = keyword if keyword is not None else keyword_for_question(question)
    order_args: dict = {}
    if order_no:
        order_args["order_no"] = order_no
    if kw:
        order_args["keyword"] = kw
    # 必须真实查订单，不能只凭用户自述的「买了 20 天」。
    try:
        order_result = invoke("query_order", order_args, ctx)
    except ToolError as exc:
        return {"ok": False, "measured_at": iso(now_shanghai()), "timezone": "Asia/Shanghai", "error_type": exc.error_type, "stage": "query_order", "tool_calls": tool_calls}
    tool_calls.append(
        {
            "tool": "query_order",
            "arguments": order_args,
            "result": order_result,
        }
    )
    orders = order_result.get("orders") or []
    if not orders:
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "error_type": "order_not_found",
            "stage": "query_order",
            "question": question,
            "tool_calls": tool_calls,
            "answer": "没有在当前用户下查到对应订单，无法仅凭口述判断能否退货。",
        }
    order = orders[0]
    try:
        search_result = retrieve(question, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        err = getattr(exc, "error_type", None) or type(exc).__name__
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "error_type": err,
            "stage": "retrieve",
            "question": question,
            "tool_calls": tool_calls,
        }
    resolution = resolve(search_result["hits"], order)
    facts = resolution["order_fields_injected"]
    retrieval_block = {
        "retriever": search_result["retriever"],
        "embedding_model": search_result["embedding_model"],
        "embedding_dim": search_result["embedding_dim"],
        "chunking_name": search_result["chunking_name"],
        "bm25": search_result["bm25"],
        "rrf_k": search_result["rrf_k"],
        "rerank": False,
        "query": search_result["query"],
        "top_k": search_result["top_k"],
    }
    conflict_block = {
        "rule": resolution["rule"],
        "n_applicable": resolution["n_applicable"],
        "n_excluded": resolution["n_excluded"],
        "applicable": [_compact_chunk(x) for x in resolution["applicable"]],
        "excluded": [_compact_chunk(x) for x in resolution["excluded"]],
    }
    try:
        gen = generate_answer(
            question=question,
            order_facts=facts,
            applicable=resolution["applicable"],
            excluded=resolution["excluded"],
            conflict_rule=resolution["rule"],
            max_evidence=max_evidence,
        )
    except GenerationError as exc:
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "error_type": exc.error_type,
            "stage": "generation",
            "question": question,
            "tool_calls": tool_calls,
            "order_fields_injected": facts,
            "retrieval": retrieval_block,
            "conflict_resolution": conflict_block,
        }

    review = check_fridge_answer(gen["answer"], facts) if "冰箱" in question else None
    return {
        "ok": True,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "session": {
            "user_id_injected_from": "server_session_context",
            "external_id": ctx.external_id,
        },
        "tool_calls": tool_calls,
        "order_fields_injected": facts,
        "retrieval": retrieval_block,
        "conflict_resolution": conflict_block,
        "model": gen["model"],
        "answer": gen["answer"],
        "usage": gen.get("usage"),
        "review": review,
        "meets_expected": (review or {}).get("meets_expected"),
    }
