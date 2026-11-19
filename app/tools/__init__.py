"""四个业务工具的注册与调度。

工具 JSON Schema 故意不含 user_id。user_id 只从服务端 SessionContext 注入。
若缺少会话上下文，即使参数里带了 user_id，也一律拒绝，且不得写入。
"""

from __future__ import annotations

from collections.abc import Callable

from app.context import MissingSessionContext, SessionContext
from app.tools.daily_report import TOOL_NAME as DAILY_REPORT_NAME
from app.tools.daily_report import TOOL_SCHEMA as DAILY_REPORT_SCHEMA
from app.tools.daily_report import daily_report
from app.tools.errors import ToolError
from app.tools.logistics import TOOL_NAME as LOGISTICS_NAME
from app.tools.logistics import TOOL_SCHEMA as LOGISTICS_SCHEMA
from app.tools.logistics import query_logistics
from app.tools.orders import TOOL_NAME as ORDER_NAME
from app.tools.orders import TOOL_SCHEMA as ORDER_SCHEMA
from app.tools.orders import query_order
from app.tools.refunds import TOOL_NAME as REFUND_NAME
from app.tools.refunds import TOOL_SCHEMA as REFUND_SCHEMA
from app.tools.refunds import refund

# 客户端不得通过工具参数传入的用户标识字段；出现时一律丢弃，且不能替代会话上下文。
_FORBIDDEN_CLIENT_KEYS = frozenset(
    {
        "user_id",
        "userId",
        "uid",
        "userid",
        "user",
        "external_id",
        "externalId",
    }
)

TOOL_SCHEMAS: list[dict] = [
    ORDER_SCHEMA,
    LOGISTICS_SCHEMA,
    REFUND_SCHEMA,
    DAILY_REPORT_SCHEMA,
]

_HANDLERS: dict[str, Callable] = {
    ORDER_NAME: query_order,
    LOGISTICS_NAME: query_logistics,
    REFUND_NAME: refund,
    DAILY_REPORT_NAME: daily_report,
}


def tool_signatures() -> dict:
    """供验收报告使用的签名摘要。"""
    out = {}
    for schema in TOOL_SCHEMAS:
        fn = schema["function"]
        out[fn["name"]] = {
            "description": fn["description"],
            "parameters": fn["parameters"],
            "injects_user_id_from": "server_session_context",
            "accepts_user_id_argument": False,
        }
    return out


def invoke(name: str, arguments: dict | None, ctx: SessionContext | None) -> dict:
    if ctx is None or getattr(ctx, "user_id", None) is None:
        raise MissingSessionContext()
    if name not in _HANDLERS:
        raise ToolError("unknown_tool")
    raw = dict(arguments or {})
    cleaned = {k: v for k, v in raw.items() if k not in _FORBIDDEN_CLIENT_KEYS}
    handler = _HANDLERS[name]
    from app.observability import observation

    with observation(name=name, as_type="tool", input={"tool": name, "arguments": cleaned}) as span:
        result = handler(ctx, **cleaned)
        # 只记录摘要，避免把整单明细重复打到追踪里。
        summary = {"tool": result.get("tool", name)}
        if "n" in result:
            summary["n"] = result.get("n")
        if "orders" in result:
            summary["order_nos"] = [o.get("order_no") for o in (result.get("orders") or [])]
        if "refunds" in result:
            summary["n_refunds"] = len(result.get("refunds") or [])
        if "report_date" in result:
            summary["report_date"] = result.get("report_date")
        if "status" in result:
            summary["status"] = result.get("status")
        span.update(output=summary)
        return result
