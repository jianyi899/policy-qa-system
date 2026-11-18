"""查物流：只读当前会话用户名下订单的本库运单与轨迹。"""

from __future__ import annotations

from app.clock import iso
from app.context import SessionContext
from app.db import connection
from app.tools.errors import ToolError

TOOL_NAME = "query_logistics"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "查询当前登录用户某笔订单的物流运单与轨迹。只读本库数据，不写库，不对接外部物流接口。",
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
        SELECT o.order_no, s.tracking_no, s.carrier, s.status,
               s.shipped_at, s.delivered_at, s.id
        FROM shipments s
        JOIN orders o ON o.id = s.order_id
        WHERE o.user_id = %s
    """
    params: list = [ctx.user_id]
    if order_no:
        sql += " AND o.order_no = %s"
        params.append(order_no.strip())
    if tracking_no:
        sql += " AND s.tracking_no = %s"
        params.append(tracking_no.strip())
    sql += " ORDER BY s.id"

    with connection(read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            shipments = []
            for row in rows:
                shipment_id = row[6]
                cur.execute(
                    """
                    SELECT event_time, location, status, description
                    FROM shipment_events
                    WHERE shipment_id = %s
                    ORDER BY event_time, id
                    """,
                    (shipment_id,),
                )
                events = [
                    {
                        "event_time": iso(ev[0]),
                        "location": ev[1],
                        "status": ev[2],
                        "description": ev[3],
                    }
                    for ev in cur.fetchall()
                ]
                shipments.append(
                    {
                        "order_no": row[0],
                        "tracking_no": row[1],
                        "carrier": row[2],
                        "status": row[3],
                        "shipped_at": iso(row[4]),
                        "delivered_at": iso(row[5]),
                        "events": events,
                    }
                )

    return {
        "ok": True,
        "tool": TOOL_NAME,
        "n": len(shipments),
        "shipments": shipments,
    }
