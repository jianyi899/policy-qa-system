"""多轮对话编排：复用会话中已查过的订单事实，并产出带句级引用的回答。

政策咨询可不查订单；追问「那这单呢」「运费呢」时不得丢掉上一轮订单事实。
工具签名仍不含 user_id，会话上下文由服务端注入。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from app.citation import evidence_sentences, score_citations
from app.clock import iso, now_shanghai
from app.conflict import context_for_resolve, explain_conflict, resolve
from app.context import SessionContext
from app.generation import GenerationError, generate_answer
from app.orchestrator import _compact_chunk, check_fridge_answer, keyword_for_question
from app.search import retrieve
from app.tools import invoke
from app.tools.errors import ToolError

ORDER_NO_RE = re.compile(r"SO-[A-Z0-9-]+", re.IGNORECASE)
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
    # 很短的追问，例如「运费呢？」「那呢」
    compact = re.sub(r"\s+", "", text)
    return len(compact) <= 12 and ("呢" in compact or compact.startswith("那"))


def extract_order_no(question: str) -> str | None:
    match = ORDER_NO_RE.search(question or "")
    return match.group(0).upper() if match else None


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
        # 种子冰箱订单类目是「大家电」，关键词「冰箱」仍视为同一单。
        if keyword == "冰箱" and last_no.startswith("SO-FRIDGE"):
            return True
        if order_no and order_no == last.get("order_no"):
            return True
        # 明确换了商品关键词且对不上上一单时，视为新单。
        return False
    return True


def _wants_order(question: str, order_no: str | None, keyword: str | None) -> bool:
    return bool(order_no or keyword or extract_order_no(question) or keyword_for_question(question))


def _order_lookup_needed_fresh(question: str, session: DialogueSession, order_no: str | None, keyword: str | None) -> bool:
    """只有需要订单且会话里还没有（或不是）这一单时，才重新查订单。"""
    if not _wants_order(question, order_no, keyword):
        return False
    if _matches_last_order(session, order_no, keyword or keyword_for_question(question)):
        return False
    return True


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
    use_cascade: bool = False,
    use_cache: bool = True,
    on_first_token=None,
) -> dict:
    """处理一轮问答。session 会写入本轮结果，已查订单可被后续轮次复用。"""
    tool_calls: list[dict] = []
    order_reused = False
    asked_no = order_no or extract_order_no(question)
    kw = keyword if keyword is not None else keyword_for_question(question)
    order = None

    if _order_lookup_needed_fresh(question, session, asked_no, kw):
        if session.ctx is None:
            return {
                "ok": False,
                "measured_at": iso(now_shanghai()),
                "timezone": "Asia/Shanghai",
                "error_type": "missing_session_context",
                "stage": "query_order",
                "question": question,
                "session_id": session.session_id,
            }
        order_args: dict = {}
        if asked_no:
            order_args["order_no"] = asked_no
        if kw:
            order_args["keyword"] = kw
        try:
            order_result = invoke("query_order", order_args, session.ctx)
        except ToolError as exc:
            return {
                "ok": False,
                "measured_at": iso(now_shanghai()),
                "timezone": "Asia/Shanghai",
                "error_type": exc.error_type,
                "stage": "query_order",
                "question": question,
                "session_id": session.session_id,
                "tool_calls": tool_calls,
            }
        tool_calls.append({"tool": "query_order", "arguments": order_args, "result": order_result, "reused": False})
        orders = order_result.get("orders") or []
        if not orders:
            return {
                "ok": False,
                "measured_at": iso(now_shanghai()),
                "timezone": "Asia/Shanghai",
                "error_type": "order_not_found",
                "stage": "query_order",
                "question": question,
                "session_id": session.session_id,
                "tool_calls": tool_calls,
                "answer": "没有在当前用户下查到对应订单，无法仅凭口述判断能否退货。",
            }
        order = orders[0]
        session.last_order = order
    elif session.last_order and (
        _wants_order(question, asked_no, kw)
        or is_followup(question, session)
        or bool(session.turns)
    ):
        # 同一会话后续轮次默认复用已查订单，除非上一分支已判定需要改查新单。
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
        search_result = retrieve(query, top_k=top_k, use_cache=use_cache)
    except Exception as exc:  # noqa: BLE001
        err = getattr(exc, "error_type", None) or type(exc).__name__
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "error_type": err,
            "stage": "retrieve",
            "question": question,
            "session_id": session.session_id,
            "tool_calls": tool_calls,
        }

    resolve_ctx = context_for_resolve(order, question)
    resolution = resolve(search_result["hits"], resolve_ctx)
    facts = resolution["order_fields_injected"]
    if order:
        session.last_order_facts = facts
    explanation = explain_conflict(resolution)
    sentences = evidence_sentences(resolution["applicable"][:max_evidence], max_sentences=max_sentences)

    from app.cascade import choose_model, retrieval_score

    score_info = retrieval_score(search_result.get("hits"))
    if use_cascade:
        cascade_decision = choose_model(score_info["score"])
        chosen_model = model or cascade_decision["model"]
    else:
        from app.generation import DEFAULT_MODEL

        cascade_decision = {
            "model": model or DEFAULT_MODEL,
            "upgraded": False,
            "score": score_info["score"],
            "threshold": None,
            "reason": "cascade_disabled",
            "signal": score_info["signal"],
        }
        chosen_model = model or DEFAULT_MODEL
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
        result = {
            "ok": True,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "question": question,
            "session_id": session.session_id,
            "order_reused": order_reused,
            "tool_calls": tool_calls,
            "order_fields_injected": facts,
            "retrieval": retrieval_block,
            "conflict_resolution": conflict_block,
            "evidence_sentences": sentences,
            "generation": "not_run",
            "cascade": cascade_block,
        }
        return result

    history = [{"question": t["question"], "answer": t["answer"]} for t in session.turns if t.get("answer")]
    try:
        from app.observability import observation

        with observation(
            name="generate_answer",
            as_type="generation",
            model=chosen_model,
            input={"question": question, "n_evidence": len(sentences)},
            model_parameters={"temperature": 0.1, "top_p": 0.8, "enable_thinking": False},
        ) as gen_span:
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
            usage = gen.get("usage") or {}
            gen_span.update(
                output={"answer": gen.get("answer"), "n_citations": len(gen.get("citations") or [])},
                usage_details={
                    "input": usage.get("input_tokens") or 0,
                    "output": usage.get("output_tokens") or 0,
                    "total": usage.get("total_tokens") or 0,
                },
            )
    except GenerationError as exc:
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "error_type": exc.error_type,
            "stage": "generation",
            "question": question,
            "session_id": session.session_id,
            "order_reused": order_reused,
            "tool_calls": tool_calls,
            "order_fields_injected": facts,
            "retrieval": retrieval_block,
            "conflict_resolution": conflict_block,
            "evidence_sentences": [{"sid": s["sid"], "doc_id": s["doc_id"], "section_path": s["section_path"], "char_start": s["char_start"], "char_end": s["char_end"], "text": s["text"]} for s in sentences],
        }

    citations = gen.get("citations") or []
    citation_score = score_citations(citations, gold_spans or []) if gold_spans is not None else None
    review = check_fridge_answer(gen["answer"], facts) if "冰箱" in question else None
    result = {
        "ok": True,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "question": question,
        "session_id": session.session_id,
        "session": {
            "user_id_injected_from": "server_session_context" if session.ctx else None,
            "external_id": getattr(session.ctx, "external_id", None) if session.ctx else None,
            "turn_index": len(session.turns) + 1,
            "order_reused": order_reused,
        },
        "order_reused": order_reused,
        "tool_calls": tool_calls,
        "order_fields_injected": facts,
        "retrieval": retrieval_block,
        "conflict_resolution": conflict_block,
        "evidence_sentences": [
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
        "model": gen["model"],
        "cascade": cascade_block,
        "answer": gen["answer"],
        "citations": citations,
        "citation_score": citation_score,
        "usage": gen.get("usage"),
        "review": review,
        "meets_expected": (review or {}).get("meets_expected"),
    }
    session.turns.append(
        {
            "question": question,
            "answer": gen["answer"],
            "citations": citations,
            "order_reused": order_reused,
            "order_no": (facts or {}).get("order_no") or (order or {}).get("order_no"),
        }
    )
    return result


def answer_question(question: str, *, ctx: SessionContext | None = None, **kwargs) -> dict:
    """单轮入口：不保留会话。评测集默认走这条。"""
    session = DialogueSession(ctx=ctx)
    return answer_turn(session, question, **kwargs)
