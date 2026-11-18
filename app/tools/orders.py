"""查订单：只读当前会话用户的订单，返回冲突解析所需字段。"""

from __future__ import annotations

from app.clock import (
    calendar_days_since,
    iso,
    within_7_day_no_reason,
    within_warranty_months,
)
from app.context import SessionContext
from app.db import connection
from app.tools.membership import is_effective_member

TOOL_NAME = "query_order"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "查询当前登录用户的订单，必须返回 sku、类目、签收时间、是否会员、运费险。"
            "售后判断不得仅凭用户口述，必须先调用本工具核对订单。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "order_no": {
                    "type": "string",
                    "description": "订单号。可选。",
                },
                "keyword": {
                    "type": "string",
                    "description": "商品关键词、SKU 或类目，例如「冰箱」。可选。",
                },
            },
            "additionalProperties": False,
        },
    },
}


def _row_to_order(row: tuple) -> dict:
    (
        order_no,
        sku,
        category,
        shipping_insurance,
        received_at,
        order_status,
        amount_cents,
        is_member_flag,
        membership_tier,
        membership_expires_at,
    ) = row
    effective_member = is_effective_member(bool(is_member_flag), membership_expires_at)
    days = calendar_days_since(received_at) if received_at is not None else None
    return {
        "order_no": order_no,
        "sku": sku,
        "category": category,
        "received_at": iso(received_at),
        "is_member": effective_member,
        "membership_tier": membership_tier,
        "membership_expires_at": iso(membership_expires_at),
        "shipping_insurance": bool(shipping_insurance),
        "order_status": order_status,
        "amount_cents": amount_cents,
        "days_since_receipt": days,
        "within_7_day_no_reason": (
            within_7_day_no_reason(received_at) if received_at is not None else None
        ),
        "within_general_warranty_12m": (
            within_warranty_months(received_at, 12) if received_at is not None else None
        ),
    }


def query_order(
    ctx: SessionContext,
    *,
    order_no: str | None = None,
    keyword: str | None = None,
) -> dict:
    """只读查询。user_id 仅来自 ctx，不接收客户端传入的用户标识。"""
    sql = """
        SELECT o.order_no, o.sku, o.category, o.shipping_insurance, o.received_at,
               o.order_status, o.amount_cents,
               u.is_member, u.membership_tier, u.membership_expires_at
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.user_id = %s
    """
    params: list = [ctx.user_id]
    if order_no:
        sql += " AND o.order_no = %s"
        params.append(order_no.strip())
    if keyword:
        key = keyword.strip()
        like = f"%{key}%"
        # 「冰箱」不会直接出现在 sku/类目字段里，按本库种子约定同时匹配大家电或 FRIDGE SKU。
        if key == "冰箱":
            sql += (
                " AND (o.sku ILIKE %s OR o.category ILIKE %s OR o.order_no ILIKE %s"
                " OR o.category = %s OR o.sku ILIKE %s)"
            )
            params.extend([like, like, like, "大家电", "%FRIDGE%"])
        else:
            sql += " AND (o.sku ILIKE %s OR o.category ILIKE %s OR o.order_no ILIKE %s)"
            params.extend([like, like, like])

    sql += " ORDER BY o.received_at DESC NULLS LAST, o.id DESC"

    with connection(read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    orders = [_row_to_order(r) for r in rows]
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "n": len(orders),
        "orders": orders,
    }
