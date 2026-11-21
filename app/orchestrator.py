"""单轮与工具入口：必须先真实查订单（若需要），注入冲突解析，再按锁定检索链路作答。

多轮与句级引用见 app.dialogue。不接入 Langfuse，不做缓存。
生成默认 qwen3.8-flash，若已标定级联阈值则可能升到 qwen-plus；未标定时不升级，并如实记录。
"""

from __future__ import annotations

import time

from app.cascade import choose_model, retrieval_score
from app.citation import evidence_sentences
from app.clock import iso, now_shanghai
from app.conflict import context_for_resolve, explain_conflict, resolve
from app.context import MissingSessionContext, SessionContext
from app.generation import GenerationError, generate_answer
from app.routing import classify, extract_order_no, keyword_for_question, wants_order
from app.search import retrieve
from app.tools import invoke, tool_signatures
from app.tools.errors import ToolError

# 主路径口语题，供脚本与验收使用。
FRIDGE_QUESTION = "这单冰箱买了 20 天质量问题还能退吗"

UNSUPPORTED_WRITE_ANSWER = (
    "第一版只支持政策问答、查订单和查物流，不支持申请退款等写操作。"
)


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
            "无法按无理由",
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
            "已过7天",
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
            "ok": ("会员" in text and any(s in text for s in ("不延长", "不会延长", "不改变", "不能延长"))),
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


def _base(question: str, ctx: SessionContext | None, started: float, **extra) -> dict:
    data = {
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "session": {
            "user_id_injected_from": "server_session_context",
            "external_id": None if ctx is None else ctx.external_id,
            "logged_in": ctx is not None and ctx.user_id is not None,
        },
    }
    data.update(extra)
    return data


def answer_after_sales(
    question: str,
    ctx: SessionContext | None,
    *,
    keyword: str | None = None,
    order_no: str | None = None,
    top_k: int = 40,
    max_evidence: int = 12,
) -> dict:
    """单轮售后问答：必须先真实查订单，再按该订单范围套政策并生成回答。"""
    started = time.perf_counter()
    if ctx is None or getattr(ctx, "user_id", None) is None:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type="missing_session_context",
            stage="query_order",
            tool_calls=[],
            answer="未选择演示账号，不能仅凭对话里自报的身份查询订单或判断能否退货。请先在页面左侧切换器中选择账号。",
        )

    tool_calls: list[dict] = []
    kw = keyword if keyword is not None else keyword_for_question(question)
    order_args: dict = {}
    if order_no:
        order_args["order_no"] = order_no
    elif extract_order_no(question):
        order_args["order_no"] = extract_order_no(question)
    if kw:
        order_args["keyword"] = kw
    # 必须真实查订单，不能只凭用户自述的「买了 20 天」，也不能使用对话里的 user_id。
    try:
        order_result = invoke("query_order", order_args, ctx)
    except ToolError as exc:
        tool_calls.append({"tool": "query_order", "arguments": order_args, "error_type": exc.error_type})
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type=exc.error_type,
            stage="query_order",
            tool_calls=tool_calls,
            answer="没有在当前所选演示账号下查到对应订单，无法仅凭口述判断能否退货。",
        )
    tool_calls.append({"tool": "query_order", "arguments": order_args, "result": order_result})
    orders = order_result.get("orders") or []
    if not orders:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type="order_not_found",
            stage="query_order",
            tool_calls=tool_calls,
            answer="没有在当前所选演示账号下查到对应订单，无法仅凭口述判断能否退货。",
        )
    order = orders[0]
    try:
        search_result = retrieve(question, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        err = getattr(exc, "error_type", None) or type(exc).__name__
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type=err,
            stage="retrieve",
            tool_calls=tool_calls,
        )
    resolution = resolve(search_result["hits"], order)
    facts = resolution["order_fields_injected"]
    explanation = explain_conflict(resolution)
    score_info = retrieval_score(search_result["hits"])
    cascade = choose_model(score_info["score"])
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
        "cascade_signal": score_info,
    }
    conflict_block = {
        "rule": resolution["rule"],
        "n_applicable": resolution["n_applicable"],
        "n_excluded": resolution["n_excluded"],
        "explanation": explanation["text"],
        "applicable": [_compact_chunk(x) for x in resolution["applicable"]],
        "excluded": [_compact_chunk(x) for x in resolution["excluded"]],
    }
    try:
        sentences = evidence_sentences(resolution["applicable"][:max_evidence], max_sentences=24)
        gen = generate_answer(
            question=question,
            order_facts=facts,
            applicable=resolution["applicable"],
            excluded=resolution["excluded"],
            conflict_rule=resolution["rule"],
            conflict_explanation=explanation["text"],
            evidence_sentences=sentences,
            model=cascade["model"],
            max_evidence=max_evidence,
        )
    except GenerationError as exc:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type=exc.error_type,
            stage="generation",
            tool_calls=tool_calls,
            order_fields_injected=facts,
            retrieval=retrieval_block,
            conflict_resolution=conflict_block,
            cascade=cascade,
            model=cascade["model"],
            cascaded=bool(cascade.get("upgraded")),
        )

    review = check_fridge_answer(gen["answer"], facts) if "冰箱" in question else None
    citations = gen.get("citations") or []
    cited = []
    for item in resolution["applicable"][:max_evidence]:
        cited.append(
            {
                "doc_id": item.get("doc_id"),
                "title": item.get("title"),
                "section_path": item.get("section_path"),
                "source_type": item.get("source_type"),
                "scope_type": item.get("scope_type"),
                "scope_value": item.get("scope_value"),
                "conflict_rank": item.get("conflict_rank"),
            }
        )
    return _base(
        question,
        ctx,
        started,
        ok=True,
        tool_calls=tool_calls,
        order_fields_injected=facts,
        retrieval=retrieval_block,
        conflict_resolution=conflict_block,
        cited_policies=cited,
        evidence_sentences=[
            {
                "sid": s["sid"],
                "doc_id": s["doc_id"],
                "section_path": s["section_path"],
                "char_start": s["char_start"],
                "char_end": s["char_end"],
                "text": s["text"],
            }
            for s in sentences
        ],
        citations=citations,
        cascade=cascade,
        model=gen["model"],
        cascaded=bool(cascade.get("upgraded")),
        answer=gen["answer"],
        usage=gen.get("usage"),
        review=review,
        meets_expected=(review or {}).get("meets_expected"),
        tool_signatures=tool_signatures(),
    )


def _tool_query(question: str, ctx: SessionContext | None, route, started: float) -> dict:
    if ctx is None or getattr(ctx, "user_id", None) is None:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            intent="tool",
            tool=route.tool,
            error_type="missing_session_context",
            stage="session",
            tool_calls=[],
            answer="未选择演示账号，不能仅凭对话里自报的身份查询订单或物流。请先在页面左侧切换器中选择账号。",
        )
    args = dict(route.arguments or {})
    try:
        result = invoke(route.tool, args, ctx)
    except ToolError as exc:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            intent="tool",
            tool=route.tool,
            error_type=exc.error_type,
            stage=route.tool,
            tool_calls=[{"tool": route.tool, "arguments": args, "error_type": exc.error_type}],
            answer="没有在当前所选演示账号下查到对应记录。",
        )
    except MissingSessionContext:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            intent="tool",
            tool=route.tool,
            error_type="missing_session_context",
            stage="session",
            tool_calls=[],
            answer="未选择演示账号，不能查询订单或物流。",
        )
    return _base(
        question,
        ctx,
        started,
        ok=True,
        intent="tool",
        tool=route.tool,
        tool_calls=[{"tool": route.tool, "arguments": args, "result": result}],
        result=result,
        answer=_format_tool_answer(route.tool, result),
    )


def _format_tool_answer(tool: str, result: dict) -> str:
    if tool == "query_order":
        orders = result.get("orders") or []
        if not orders:
            return "当前所选演示账号下没有查到订单。"
        lines = [f"查到 {len(orders)} 笔订单："]
        for order in orders:
            lines.append(
                f"- {order.get('order_no')}：SKU={order.get('sku')}，类目={order.get('category')}，"
                f"签收时间={order.get('received_at')}，是否会员={order.get('is_member')}，"
                f"运费险={order.get('shipping_insurance')}"
            )
        return "\n".join(lines)
    if tool == "query_logistics":
        shipments = result.get("shipments") or []
        if not shipments:
            return "当前所选演示账号下没有查到物流运单。"
        lines = [f"查到 {len(shipments)} 条运单："]
        for item in shipments:
            lines.append(
                f"- 订单 {item.get('order_no')}，运单 {item.get('tracking_no')}，"
                f"承运商={item.get('carrier')}，状态={item.get('status')}，"
                f"发运={item.get('shipped_at')}，签收={item.get('delivered_at')}"
            )
        return "\n".join(lines)
    return "已完成查询。"


def answer_policy_only(
    question: str,
    ctx: SessionContext | None = None,
    *,
    top_k: int = 40,
    max_evidence: int = 12,
) -> dict:
    """不绑定订单的政策咨询：用问句推断的范围做冲突解析，不调用查订单。"""
    started = time.perf_counter()
    try:
        search_result = retrieve(question, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        err = getattr(exc, "error_type", None) or type(exc).__name__
        return _base(question, ctx, started, ok=False, error_type=err, stage="retrieve", intent="policy")
    scope_ctx = context_for_resolve({}, question)
    resolution = resolve(search_result["hits"], scope_ctx)
    facts = resolution["order_fields_injected"]
    explanation = explain_conflict(resolution)
    score_info = retrieval_score(search_result["hits"])
    cascade = choose_model(score_info["score"])
    try:
        sentences = evidence_sentences(resolution["applicable"][:max_evidence], max_sentences=24)
        gen = generate_answer(
            question=question,
            order_facts=facts,
            applicable=resolution["applicable"],
            excluded=resolution["excluded"],
            conflict_rule=resolution["rule"],
            conflict_explanation=explanation["text"],
            evidence_sentences=sentences,
            model=cascade["model"],
            max_evidence=max_evidence,
        )
    except GenerationError as exc:
        return _base(
            question,
            ctx,
            started,
            ok=False,
            error_type=exc.error_type,
            stage="generation",
            intent="policy",
            cascade=cascade,
            model=cascade["model"],
            cascaded=bool(cascade.get("upgraded")),
        )
    citations = gen.get("citations") or []
    cited = [
        {
            "doc_id": item.get("doc_id"),
            "title": item.get("title"),
            "section_path": item.get("section_path"),
            "conflict_rank": item.get("conflict_rank"),
        }
        for item in resolution["applicable"][:max_evidence]
    ]
    return _base(
        question,
        ctx,
        started,
        ok=True,
        intent="policy",
        tool_calls=[],
        order_fields_injected=facts,
        conflict_resolution={
            "rule": resolution["rule"],
            "n_applicable": resolution["n_applicable"],
            "n_excluded": resolution["n_excluded"],
            "explanation": explanation["text"],
            "has_finer_scope": explanation["has_finer_scope"],
            "by_scope_doc_ids": explanation["by_scope_doc_ids"],
            "applicable": [_compact_chunk(x) for x in resolution["applicable"]],
            "excluded": [_compact_chunk(x) for x in resolution["excluded"]],
        },
        cited_policies=cited,
        evidence_sentences=[
            {
                "sid": s["sid"],
                "doc_id": s["doc_id"],
                "section_path": s["section_path"],
                "char_start": s["char_start"],
                "char_end": s["char_end"],
                "text": s["text"],
            }
            for s in sentences
        ],
        citations=citations,
        cascade=cascade,
        model=gen["model"],
        cascaded=bool(cascade.get("upgraded")),
        answer=gen["answer"],
        usage=gen.get("usage"),
    )


def handle_question(
    question: str,
    ctx: SessionContext | None = None,
    *,
    top_k: int = 40,
    max_evidence: int = 12,
) -> dict:
    """单轮入口：按问题路由到查订单、查物流或政策问答。政策路径产出句级引用。不从对话文本解析身份。多轮请使用 DialogueSession。"""
    started = time.perf_counter()
    route = classify(question)
    if route.intent == "unsupported_write":
        return _base(
            question,
            ctx,
            started,
            ok=True,
            intent="unsupported_write",
            answer=UNSUPPORTED_WRITE_ANSWER,
            tool_calls=[],
        )
    if route.intent == "tool":
        return _tool_query(question, ctx, route, started)
    # 政策路径（含需要先查订单的售后题）走多轮模块的单轮入口，统一产出句级引用。
    from app.dialogue import answer_question

    return answer_question(
        question,
        ctx=ctx,
        keyword=keyword_for_question(question),
        order_no=extract_order_no(question),
        top_k=top_k,
        max_evidence=max_evidence,
    )
