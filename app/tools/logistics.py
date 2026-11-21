"""查物流：只读当前会话用户名下订单的本库运单。第一版只查 logistics 表，不写库，不对接外部接口。"""

from __future__ import annotations

from app.clock import iso, parse_datetime
from app.context import SessionContext
from app.db import connection
from app.tools.errors import ToolError

TOOL_NAME = "query_logistics"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "查询当前登录用户某笔订单的物流运单。只读本库数据，不写库，不对接外部物流接口。",
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {"type": "string", "description": "订单号。与运单号至少提供一个。"},
                "tracking_no": {"type": "string", "description": "运单号。与订单号至少提供一个。"},
            },
            "additionalProperties": False,
        },
    },
}


def query_logistics(
    ctx: SessionContext,
    *,
    order_no: str | None = None,
    tracking_no: str | None = None,
) -> dict:
    if not (order_no or tracking_no):
        raise ToolError("missing_order_or_tracking")

    sql = """
        SELECT o.order_no, l.tracking_no, l.carrier, l.status,
               l.shipped_at, l.delivered_at
        FROM logistics l
        JOIN orders o ON o.id = l.order_id
        WHERE o.user_id = ?
    """
    params: list = [ctx.user_id]
    if order_no:
        sql += " AND o.order_no = ?"
        params.append(order_no.strip())
    if tracking_no:
        sql += " AND l.tracking_no = ?"
        params.append(tracking_no.strip())
    sql += " ORDER BY l.id"

    with connection(read_only=True) as conn:
        rows = conn.execute(sql, params).fetchall()

    shipments = [
        {
            "order_no": row["order_no"],
            "tracking_no": row["tracking_no"],
            "carrier": row["carrier"],
            "status": row["status"],
            "shipped_at": iso(parse_datetime(row["shipped_at"])),
            "delivered_at": iso(parse_datetime(row["delivered_at"])),
        }
        for row in rows
    ]
    if not shipments:
        raise ToolError("logistics_not_found")
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "n": len(shipments),
        "shipments": shipments,
    }
