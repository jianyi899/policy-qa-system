"""第六段对外入口：路由、缓存、级联与追踪。

工具类在路由层直接调度，绕过政策问答缓存和检索缓存。
纯政策问答可命中问答缓存；检索结果另有精确查询缓存。
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Iterator

from app.cache import (
    answer_key,
    compact_answer_payload,
    get_json,
    set_json,
)
from app.clock import iso, now_shanghai
from app.context import SessionContext
from app.dialogue import DialogueSession, answer_turn
from app.observability import observation
from app.routing import Route, classify
from app.tools import invoke
from app.tools.errors import ToolError


def _resolve_tool_args(route: Route) -> dict:
    args = dict(route.arguments or {})
    offset = args.pop("report_date_offset_days", None)
    if offset is not None and not args.get("report_date"):
        day = now_shanghai().date() + timedelta(days=int(offset))
        args["report_date"] = day.isoformat()
    return args


def _tool_answer_text(tool: str, result: dict) -> str:
    if tool == "query_order":
        orders = result.get("orders") or []
        if not orders:
            return "没有在当前用户下查到对应订单。"
        parts = []
        for order in orders:
            parts.append(
                f"订单 {order.get('order_no')}，SKU {order.get('sku')}，类目 {order.get('category')}，"
                f"签收时间 {order.get('received_at')}，会员 {order.get('is_member')}。"
            )
        return "已查到订单：" + " ".join(parts)
    if tool == "query_logistics":
        return f"物流查询完成，共 {result.get('n') or 0} 条运单。"
    if tool == "refund":
        if result.get("action") == "apply":
            return f"退款申请结果：{result.get('status') or result.get('refund_no') or '已处理'}。"
        return f"退款查询完成，共 {result.get('n') or 0} 条记录。"
    if tool == "daily_report":
        return f"日报（{result.get('report_date') or ''}）已处理。"
    return "工具调用完成。"


def _dispatch_tool(route: Route, ctx: SessionContext | None) -> dict:
    started = time.perf_counter()
    if ctx is None:
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "intent": "tool",
            "tool": route.tool,
            "cacheable": False,
            "cache_hit": False,
            "cache_bypassed": True,
            "error_type": "missing_session_context",
            "stage": "tool",
            "latency_ms": (time.perf_counter() - started) * 1000,
        }
    args = _resolve_tool_args(route)
    try:
        result = invoke(route.tool, args, ctx)
    except ToolError as exc:
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "intent": "tool",
            "tool": route.tool,
            "tool_arguments": args,
            "cacheable": False,
            "cache_hit": False,
            "cache_bypassed": True,
            "error_type": exc.error_type,
            "stage": "tool",
            "latency_ms": (time.perf_counter() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "intent": "tool",
            "tool": route.tool,
            "tool_arguments": args,
            "cacheable": False,
            "cache_hit": False,
            "cache_bypassed": True,
            "error_type": type(exc).__name__,
            "stage": "tool",
            "latency_ms": (time.perf_counter() - started) * 1000,
        }
    return {
        "ok": True,
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "intent": "tool",
        "tool": route.tool,
        "tool_arguments": args,
        "tool_result": result,
        "answer": _tool_answer_text(route.tool or "", result),
        "cacheable": False,
        "cache_hit": False,
        "cache_bypassed": True,
        "model": None,
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "latency_ms": (time.perf_counter() - started) * 1000,
    }


def _attach_timing(result: dict, *, started: float, first_token_at: float | None) -> dict:
    now = time.perf_counter()
    result["latency_ms"] = (now - started) * 1000
    if first_token_at is not None:
        result["first_token_ms"] = (first_token_at - started) * 1000
    return result


def handle_ask(
    question: str,
    *,
    ctx: SessionContext | None = None,
    session: DialogueSession | None = None,
    use_cascade: bool = True,
    use_cache: bool = True,
    generate: bool = True,
    gold_spans: list[dict] | None = None,
    model: str | None = None,
    on_first_token=None,
    precomputed_answer: dict | None = None,
    lookup_only: bool = False,
) -> dict:
    """统一入口。precomputed_answer 仅供评测回填已实测答案，避免为填缓存再计一次生成费。

    lookup_only=True 时只查问答缓存并返回是否命中，不检索、不生成、不写缓存。
    """
    started = time.perf_counter()
    first_token_at: list[float] = []

    def _note_first(_piece=None) -> None:
        if not first_token_at:
            first_token_at.append(time.perf_counter())
        if on_first_token is not None:
            on_first_token(_piece)

    route = classify(question)
    with observation(
        name="handle_ask",
        as_type="span",
        input={"question": question, "intent": route.intent, "reason": route.reason},
        metadata={"cacheable": route.cacheable, "tool": route.tool},
    ) as root:
        if route.intent == "tool":
            result = _dispatch_tool(route, ctx)
            _note_first((result.get("answer") or "")[:1] or " ")
            _attach_timing(result, started=started, first_token_at=first_token_at[0] if first_token_at else None)
            result["route"] = {"intent": route.intent, "tool": route.tool, "reason": route.reason, "cacheable": False}
            result["trace_id"] = getattr(root, "trace_id", None)
            root.update(output={"ok": result.get("ok"), "intent": "tool", "tool": route.tool, "cache_bypassed": True})
            return result

        cache_key = answer_key(question) if (use_cache and route.cacheable) else None
        if cache_key:
            cached = get_json(cache_key)
            if cached and cached.get("answer"):
                result = dict(cached)
                result["intent"] = "policy"
                result["cacheable"] = True
                result["cache_hit"] = True
                result["cache_bypassed"] = False
                result["cache_key"] = cache_key
                result["route"] = {
                    "intent": route.intent,
                    "reason": route.reason,
                    "cacheable": True,
                }
                # 命中缓存不消耗新的生成 token
                result["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                result["cached_usage"] = cached.get("usage")
                result["measured_at"] = iso(now_shanghai())
                result["timezone"] = "Asia/Shanghai"
                _note_first((result.get("answer") or "")[:1] or " ")
                _attach_timing(result, started=started, first_token_at=first_token_at[0] if first_token_at else None)
                result["trace_id"] = getattr(root, "trace_id", None)
                root.update(output={"ok": True, "intent": "policy", "cache_hit": True})
                return result

        if lookup_only:
            result = {
                "ok": True,
                "question": question,
                "intent": "policy",
                "cacheable": bool(route.cacheable),
                "cache_hit": False,
                "cache_bypassed": False,
                "route": {"intent": route.intent, "reason": route.reason, "cacheable": route.cacheable},
                "answer": None,
                "measured_at": iso(now_shanghai()),
                "timezone": "Asia/Shanghai",
            }
            result["trace_id"] = getattr(root, "trace_id", None)
            root.update(output={"ok": True, "intent": "policy", "cache_hit": False, "lookup_only": True})
            _attach_timing(result, started=started, first_token_at=None)
            return result

        if precomputed_answer is not None:
            result = dict(precomputed_answer)
            result["ok"] = bool(result.get("ok", True))
            result["question"] = question
            result["intent"] = "policy"
            result["cacheable"] = bool(route.cacheable)
            result["cache_hit"] = False
            result["cache_bypassed"] = False
            result["route"] = {"intent": route.intent, "reason": route.reason, "cacheable": route.cacheable}
            result["measured_at"] = iso(now_shanghai())
            result["timezone"] = "Asia/Shanghai"
            if cache_key and route.cacheable and result.get("answer"):
                set_json(cache_key, compact_answer_payload(result))
                result["cache_key"] = cache_key
            _note_first((result.get("answer") or "")[:1] or " ")
            _attach_timing(result, started=started, first_token_at=first_token_at[0] if first_token_at else None)
            result["trace_id"] = getattr(root, "trace_id", None)
            root.update(output={"ok": result.get("ok"), "intent": "policy", "cache_hit": False, "precomputed": True})
            return result

        sess = session or DialogueSession(ctx=ctx)
        result = answer_turn(
            sess,
            question,
            generate=generate,
            gold_spans=gold_spans,
            model=model,
            use_cascade=use_cascade,
            use_cache=use_cache,
            on_first_token=_note_first,
        )
        result["intent"] = "policy"
        result["cacheable"] = bool(route.cacheable)
        result["cache_hit"] = False
        result["cache_bypassed"] = False
        result["route"] = {"intent": route.intent, "reason": route.reason, "cacheable": route.cacheable}
        if cache_key and route.cacheable and result.get("ok") and result.get("answer"):
            set_json(cache_key, compact_answer_payload(result))
            result["cache_key"] = cache_key
        if not first_token_at and result.get("answer"):
            _note_first((result.get("answer") or "")[:1])
        _attach_timing(result, started=started, first_token_at=first_token_at[0] if first_token_at else None)
        result["trace_id"] = getattr(root, "trace_id", None)
        root.update(
            output={
                "ok": result.get("ok"),
                "intent": "policy",
                "cache_hit": False,
                "model": result.get("model"),
                "upgraded": (result.get("cascade") or {}).get("upgraded"),
            }
        )
        return result


def iter_answer_chunks(result: dict) -> Iterator[str]:
    text = result.get("answer") or ""
    if not text:
        return
    # 缓存命中时按短块写出，便于测量首 token；内容本身已在内存中。
    yield text
