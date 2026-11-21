"""查询工具的注册与调度。

工具 JSON Schema 故意不含 user_id。user_id 只从服务端 SessionContext 注入。
若缺少会话上下文，即使参数里带了 user_id，也一律拒绝，且不得写入。
第一版只提供查订单、查物流，不实现申请退款等写操作。
"""

from __future__ import annotations

from collections.abc import Callable

from app.context import MissingSessionContext, SessionContext
from app.tools.errors import ToolError
from app.tools.logistics import TOOL_NAME as LOGISTICS_NAME
from app.tools.logistics import TOOL_SCHEMA as LOGISTICS_SCHEMA
from app.tools.logistics import query_logistics
from app.tools.orders import TOOL_NAME as ORDER_NAME
from app.tools.orders import TOOL_SCHEMA as ORDER_SCHEMA
from app.tools.orders import query_order

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
]

_HANDLERS: dict[str, Callable] = {
    ORDER_NAME: query_order,
    LOGISTICS_NAME: query_logistics,
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
    result = handler(ctx, **cleaned)
    return result
