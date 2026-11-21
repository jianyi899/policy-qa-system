"""多轮对话编排：复用会话中已查过的订单事实，并产出带句级引用的回答。

政策咨询可不查订单；追问「那这单呢」「运费呢」时不得丢掉上一轮订单事实。
需要时在后续轮次才查订单。工具签名仍不含 user_id，会话上下文由服务端注入。
不接入 Langfuse，不做缓存。级联沿用已有 choose_model：未标定阈值时不升级。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

from app.cascade import choose_model, retrieval_score
from app.citation import evidence_sentences, score_citations
from app.clock import iso, now_shanghai
from app.conflict import context_for_resolve, explain_conflict, resolve
from app.context import SessionContext
from app.generation import GenerationError, generate_answer
from app.orchestrator import (
    UNSUPPORTED_WRITE_ANSWER,
    _compact_chunk,
    _format_tool_answer,
    check_fridge_answer,
)
from app.routing import classify, extract_order_no, keyword_for_question
from app.search import retrieve
from app.tools import invoke
from app.tools.errors import ToolError

FOLLOWUP_HINTS = (
    "这单",
    "该单",
    "此单",
    "那这",
    "那运费",
    "运费呢",
    "那质量",
    "刚才",
    "上面",
    "那会员",
    "那保修",
    "那质保",
    "那还能",
    "那可以",
    "那不能",
    "那我这",
    "同一单",
    "同一订单",
)


@dataclass
class DialogueSession:
    ctx: SessionContext | None = None
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    turns: list[dict] = field(default_factory=list)
    last_order: dict | None = None
    last_order_facts: dict | None = None


def is_followup(question: str, session: DialogueSession) -> bool:
    if not session.turns:
        return False
    text = question or ""
    if any(h in text for h in FOLLOWUP_HINTS):
        return True
    compact = re.sub(r"\s+", "", text)
    return len(compact) <= 12 and ("呢" in compact or compact.startswith("那"))


def retrieval_query(question: str, session: DialogueSession) -> str:
    if is_followup(question, session):
        prev = " ".join(t.get("question") or "" for t in session.turns[-2:])
        return f"{prev} {question}".strip()
    return question


def _matches_last_order(session: DialogueSession, order_no: str | None, keyword: str | None) -> bool:
    last = session.last_order
    if not last:
        return False
    if order_no and order_no != last.get("order_no"):
        return False
    if keyword:
        last_cat = str(last.get("category") or "")
        last_sku = str(last.get("sku") or "")
        last_no = str(last.get("order_no") or "")
        if keyword in last_cat or keyword in last_sku or keyword in last_no:
            return True
        if keyword == "冰箱" and last_no.startswith("SO-FRIDGE"):
            return True
        return False
    return True


def _wants_order(question: str, order_no: str | None, keyword: str | None) -> bool:
    return bool(order_no or keyword or extract_order_no(question) or keyword_for_question(question))


def _order_lookup_needed_fresh(
    question: str, session: DialogueSession, order_no: str | None, keyword: str | None
) -> bool:
    """只有需要订单且会话里还没有（或不是）这一单时，才重新查订单。"""
    if not _wants_order(question, order_no, keyword):
        return False
    if _matches_last_order(session, order_no, keyword or keyword_for_question(question)):
        return False
    return True


def _should_reuse_order(question: str, session: DialogueSession, order_no: str | None, keyword: str | None) -> bool:
    if not session.last_order:
        return False
    if is_followup(question, session):
        return True
    if _wants_order(question, order_no, keyword) and _matches_last_order(
        session, order_no, keyword or keyword_for_question(question)
    ):
        return True
    return False


def _fail(session: DialogueSession, question: str, started: float, **extra) -> dict:
    data = {
        "ok": False,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "session_id": session.session_id,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "session": {
            "user_id_injected_from": "server_session_context" if session.ctx else None,
            "external_id": getattr(session.ctx, "external_id", None) if session.ctx else None,
            "logged_in": session.ctx is not None and getattr(session.ctx, "user_id", None) is not None,
            "turn_index": len(session.turns) + 1,
        },
    }
    data.update(extra)
    return data


def _record_turn(session: DialogueSession, question: str, result: dict) -> None:
    session.turns.append(
        {
            "question": question,
            "answer": result.get("answer"),
            "citations": result.get("citations") or [],
            "order_reused": result.get("order_reused"),
            "order_no": (result.get("order_fields_injected") or {}).get("order_no")
            or ((session.last_order or {}).get("order_no")),
            "intent": result.get("intent"),
        }
    )


def _compact_sentences(sentences: list[dict]) -> list[dict]:
    return [
        {
            "sid": s["sid"],
            "doc_id": s["doc_id"],
            "section_path": s["section_path"],
            "char_start": s["char_start"],
            "char_end": s["char_end"],
            "text": s["text"],
        }
        for s in sentences
    ]


def answer_turn(
    session: DialogueSession,
    question: str,
    *,
    order_no: str | None = None,
    keyword: str | None = None,
    top_k: int = 40,
    max_evidence: int = 12,
    max_sentences: int = 24,
    generate: bool = True,
    gold_spans: list[dict] | None = None,
    model: str | None = None,
    on_first_token=None,
) -> dict:
    """处理一轮问答。session 会写入本轮结果，已查订单可被后续轮次复用。"""
    started = time.perf_counter()
    route = classify(question)
    if route.intent == "unsupported_write":
        result = {
            "ok": True,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "question": question,
            "session_id": session.session_id,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "intent": "unsupported_write",
            "answer": UNSUPPORTED_WRITE_ANSWER,
            "tool_calls": [],
            "order_reused": False,
            "session": {
                "user_id_injected_from": "server_session_context" if session.ctx else None,
                "external_id": getattr(session.ctx, "external_id", None) if session.ctx else None,
                "turn_index": len(session.turns) + 1,
                "order_reused": False,
            },
        }
        _record_turn(session, question, result)
        return result
    if route.intent == "tool":
        return _tool_turn(session, question, route, started)

    tool_calls: list[dict] = []
    order_reused = False
    asked_no = order_no or extract_order_no(question)
    kw = keyword if keyword is not None else keyword_for_question(question)
    order = None

    if _order_lookup_needed_fresh(question, session, asked_no, kw):
        if session.ctx is None or getattr(session.ctx, "user_id", None) is None:
            result = _fail(
                session,
                question,
                started,
                error_type="missing_session_context",
                stage="query_order",
                tool_calls=[],
                answer="未选择演示账号，不能仅凭对话里自报的身份查询订单或判断能否退货。请先在页面左侧切换器中选择账号。",
            )
            return result
        order_args: dict = {}
        if asked_no:
            order_args["order_no"] = asked_no
        if kw:
            order_args["keyword"] = kw
        try:
            order_result = invoke("query_order", order_args, session.ctx)
        except ToolError as exc:
            return _fail(
                session,
                question,
                started,
                error_type=exc.error_type,
                stage="query_order",
                tool_calls=[{"tool": "query_order", "arguments": order_args, "error_type": exc.error_type}],
                answer="没有在当前所选演示账号下查到对应订单，无法仅凭口述判断能否退货。",
            )
        tool_calls.append({"tool": "query_order", "arguments": order_args, "result": order_result, "reused": False})
        orders = order_result.get("orders") or []
        if not orders:
            return _fail(
                session,
                question,
                started,
                error_type="order_not_found",
                stage="query_order",
                tool_calls=tool_calls,
                answer="没有在当前所选演示账号下查到对应订单，无法仅凭口述判断能否退货。",
            )
        order = orders[0]
        session.last_order = order
    elif _should_reuse_order(question, session, asked_no, kw):
        order = session.last_order
        order_reused = True
        tool_calls.append(
            {
                "tool": "query_order",
                "arguments": {"reused": True, "order_no": order.get("order_no")},
                "result": {"reused": True, "order": order},
                "reused": True,
            }
        )

    query = retrieval_query(question, session)
    try:
        search_result = retrieve(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001
        err = getattr(exc, "error_type", None) or type(exc).__name__
        return _fail(
            session,
            question,
            started,
            error_type=err,
            stage="retrieve",
            tool_calls=tool_calls,
            order_reused=order_reused,
        )

    resolve_ctx = context_for_resolve(order, question)
    resolution = resolve(search_result["hits"], resolve_ctx)
    facts = resolution["order_fields_injected"]
    if order:
        session.last_order_facts = facts
    explanation = explain_conflict(resolution)
    sentences = evidence_sentences(resolution["applicable"][:max_evidence], max_sentences=max_sentences)

    score_info = retrieval_score(search_result.get("hits"))
    cascade_decision = choose_model(score_info["score"])
    if model:
        chosen_model = model
        cascade_decision = {**cascade_decision, "model": model, "reason": cascade_decision.get("reason")}
    else:
        chosen_model = cascade_decision["model"]
    cascade_block = {**cascade_decision, **score_info}

    retrieval_block = {
        "retriever": search_result["retriever"],
        "embedding_model": search_result["embedding_model"],
        "embedding_dim": search_result["embedding_dim"],
        "chunking_name": search_result["chunking_name"],
        "bm25": search_result["bm25"],
        "rrf_k": search_result["rrf_k"],
        "rerank": False,
        "query": search_result["query"],
        "expanded_query": query if query != question else None,
        "top_k": search_result["top_k"],
        "cascade_signal": score_info,
    }
    conflict_block = {
        "rule": resolution["rule"],
        "explanation": explanation["text"],
        "has_finer_scope": explanation["has_finer_scope"],
        "by_scope_doc_ids": explanation["by_scope_doc_ids"],
        "n_applicable": resolution["n_applicable"],
        "n_excluded": resolution["n_excluded"],
        "applicable": [_compact_chunk(x) for x in resolution["applicable"]],
        "excluded": [_compact_chunk(x) for x in resolution["excluded"]],
    }

    if not generate:
        return {
            "ok": True,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "question": question,
            "session_id": session.session_id,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "intent": "policy",
            "order_reused": order_reused,
            "tool_calls": tool_calls,
            "order_fields_injected": facts,
            "retrieval": retrieval_block,
            "conflict_resolution": conflict_block,
            "evidence_sentences": _compact_sentences(sentences),
            "generation": "not_run",
            "cascade": cascade_block,
            "model": chosen_model,
            "cascaded": bool(cascade_decision.get("upgraded")),
        }

    history = [{"question": t["question"], "answer": t["answer"]} for t in session.turns if t.get("answer")]
    try:
        gen = generate_answer(
            question=question,
            order_facts=facts,
            applicable=resolution["applicable"],
            excluded=resolution["excluded"],
            conflict_rule=resolution["rule"],
            conflict_explanation=explanation["text"],
            evidence_sentences=sentences,
            history=history,
            model=chosen_model,
            max_evidence=max_evidence,
            on_first_token=on_first_token,
        )
    except GenerationError as exc:
        return _fail(
            session,
            question,
            started,
            error_type=exc.error_type,
            stage="generation",
            intent="policy",
            order_reused=order_reused,
            tool_calls=tool_calls,
            order_fields_injected=facts,
            retrieval=retrieval_block,
            conflict_resolution=conflict_block,
            evidence_sentences=_compact_sentences(sentences),
            cascade=cascade_block,
            model=chosen_model,
            cascaded=bool(cascade_decision.get("upgraded")),
        )

    citations = gen.get("citations") or []
    citation_score = score_citations(citations, gold_spans or []) if gold_spans is not None else None
    review = check_fridge_answer(gen["answer"], facts) if "冰箱" in question else None
    result = {
        "ok": True,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "session_id": session.session_id,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "intent": "policy",
        "session": {
            "user_id_injected_from": "server_session_context" if session.ctx else None,
            "external_id": getattr(session.ctx, "external_id", None) if session.ctx else None,
            "logged_in": session.ctx is not None and getattr(session.ctx, "user_id", None) is not None,
            "turn_index": len(session.turns) + 1,
            "order_reused": order_reused,
        },
        "order_reused": order_reused,
        "tool_calls": tool_calls,
        "order_fields_injected": facts,
        "retrieval": retrieval_block,
        "conflict_resolution": conflict_block,
        "cited_policies": [
            {
                "doc_id": item.get("doc_id"),
                "title": item.get("title"),
                "section_path": item.get("section_path"),
                "source_type": item.get("source_type"),
                "scope_type": item.get("scope_type"),
                "scope_value": item.get("scope_value"),
                "conflict_rank": item.get("conflict_rank"),
            }
            for item in resolution["applicable"][:max_evidence]
        ],
        "evidence_sentences": _compact_sentences(sentences),
        "model": gen["model"],
        "cascade": cascade_block,
        "cascaded": bool(cascade_decision.get("upgraded")),
        "answer": gen["answer"],
        "citations": citations,
        "citation_score": citation_score,
        "usage": gen.get("usage"),
        "review": review,
        "meets_expected": (review or {}).get("meets_expected"),
    }
    _record_turn(session, question, result)
    return result


def _tool_turn(session: DialogueSession, question: str, route, started: float) -> dict:
    if session.ctx is None or getattr(session.ctx, "user_id", None) is None:
        return _fail(
            session,
            question,
            started,
            intent="tool",
            tool=route.tool,
            error_type="missing_session_context",
            stage="session",
            tool_calls=[],
            answer="未选择演示账号，不能仅凭对话里自报的身份查询订单或物流。请先在页面左侧切换器中选择账号。",
        )
    args = dict(route.arguments or {})
    try:
        result = invoke(route.tool, args, session.ctx)
    except ToolError as exc:
        return _fail(
            session,
            question,
            started,
            intent="tool",
            tool=route.tool,
            error_type=exc.error_type,
            stage=route.tool,
            tool_calls=[{"tool": route.tool, "arguments": args, "error_type": exc.error_type}],
            answer="没有在当前所选演示账号下查到对应记录。",
        )
    if route.tool == "query_order":
        orders = result.get("orders") or []
        if orders:
            session.last_order = orders[0]
    answer = _format_tool_answer(route.tool, result)
    payload = {
        "ok": True,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "session_id": session.session_id,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "intent": "tool",
        "tool": route.tool,
        "tool_calls": [{"tool": route.tool, "arguments": args, "result": result, "reused": False}],
        "result": result,
        "answer": answer,
        "order_reused": False,
        "order_fields_injected": (result.get("orders") or [None])[0] if route.tool == "query_order" else None,
        "session": {
            "user_id_injected_from": "server_session_context",
            "external_id": getattr(session.ctx, "external_id", None),
            "logged_in": True,
            "turn_index": len(session.turns) + 1,
            "order_reused": False,
        },
    }
    _record_turn(session, question, payload)
    return payload


def answer_question(question: str, *, ctx: SessionContext | None = None, **kwargs) -> dict:
    """单轮入口：不保留会话。评测集默认走这条。"""
    session = DialogueSession(ctx=ctx)
    return answer_turn(session, question, **kwargs)
