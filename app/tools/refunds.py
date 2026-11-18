"""申请或查询退款。进行中退款的唯一性由数据库约束 ux_refunds_one_open_per_order 保证。"""

from __future__ import annotations

import uuid

from psycopg.errors import UniqueViolation

from app.clock import iso
from app.context import SessionContext
from app.db import connection
from app.tools.errors import ToolError

TOOL_NAME = "refund"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "申请或查询当前登录用户的退款。"
            "查询为只读；申请时同一订单只能有一笔进行中的退款（由数据库唯一约束保证）。"
            "幂等键用于识别同一次提交的重试，不要把日期或理由写入幂等键。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["query", "apply"],
                    "description": "query 查询，apply 申请。",
                },
                "order_no": {"type": "string", "description": "订单号。申请时必填；查询时与退款单号至少填一个。"},
                "refund_no": {"type": "string", "description": "退款单号，查询时可选。"},
                "reason": {
                    "type": "string",
                    "description": "退款理由，仅作记录，不作为幂等键的一部分。",
                },
                "amount_cents": {"type": "integer", "description": "退款金额（分）。可选，默认按订单金额。"},
                "idempotency_key": {
                    "type": "string",
                    "description": "幂等键。重试请传同一值；不得包含当日日期或理由。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

# 生成默认幂等键时只用随机标识，绝不拼入日期或理由。
def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _refund_dict(row: tuple) -> dict:
    (
        refund_no,
        order_no,
        status,
        reason,
        amount_cents,
        idempotency_key,
        created_at,
        updated_at,
    ) = row
    return {
        "refund_no": refund_no,
        "order_no": order_no,
        "status": status,
        "reason": reason,
        "amount_cents": amount_cents,
        "idempotency_key": idempotency_key,
        "created_at": iso(created_at),
        "updated_at": iso(updated_at),
    }


_REFUND_SELECT = """
    SELECT r.refund_no, o.order_no, r.status, r.reason, r.amount_cents,
           r.idempotency_key, r.created_at, r.updated_at
    FROM refunds r
    JOIN orders o ON o.id = r.order_id
    WHERE o.user_id = %s
"""


def _query(ctx: SessionContext, *, order_no: str | None, refund_no: str | None) -> dict:
    if not (order_no or refund_no):
        raise ToolError("missing_order_or_refund_no")
    sql = _REFUND_SELECT
    params: list = [ctx.user_id]
    if order_no:
        sql += " AND o.order_no = %s"
        params.append(order_no.strip())
    if refund_no:
        sql += " AND r.refund_no = %s"
        params.append(refund_no.strip())
    sql += " ORDER BY r.id"
    with connection(read_only=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "action": "query",
        "n": len(rows),
        "refunds": [_refund_dict(r) for r in rows],
    }


def _fetch_by_idempotency(cur, user_id: int, key: str) -> dict | None:
    cur.execute(
        _REFUND_SELECT + " AND r.idempotency_key = %s",
        (user_id, key),
    )
    row = cur.fetchone()
    return _refund_dict(row) if row else None


def _owned_order(cur, user_id: int, order_no: str) -> tuple[int, int] | None:
    cur.execute(
        """
        SELECT o.id, o.amount_cents
        FROM orders o
        WHERE o.user_id = %s AND o.order_no = %s
        """,
        (user_id, order_no),
    )
    row = cur.fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1] or 0)


def _apply(
    ctx: SessionContext,
    *,
    order_no: str | None,
    reason: str | None,
    amount_cents: int | None,
    idempotency_key: str | None,
) -> dict:
    if not order_no:
        raise ToolError("missing_order_no")
    order_no = order_no.strip()
    # 若调用方未提供幂等键，则生成随机键；明确不把当日或理由写进去。
    key = (idempotency_key or "").strip() or new_idempotency_key()
    if reason and reason.strip() and reason.strip() == key:
        # 防御：不允许把理由直接当作幂等键。
        raise ToolError("idempotency_key_must_not_be_reason")

    with connection(read_only=False) as conn:
        with conn.cursor() as cur:
            existing = _fetch_by_idempotency(cur, ctx.user_id, key)
            if existing:
                return {
                    "ok": True,
                    "tool": TOOL_NAME,
                    "action": "apply",
                    "idempotent_replay": True,
                    "refund": existing,
                }

            owned = _owned_order(cur, ctx.user_id, order_no)
            if owned is None:
                raise ToolError("order_not_found")
            order_id, order_amount = owned
            amount = amount_cents if amount_cents is not None else order_amount
            refund_no = f"RF-{uuid.uuid4().hex[:16]}"
            try:
                cur.execute(
                    """
                    INSERT INTO refunds (
                        refund_no, order_id, status, reason, amount_cents, idempotency_key
                    ) VALUES (
                        %s, %s, 'pending', %s, %s, %s
                    )
                    RETURNING refund_no, status, reason, amount_cents, idempotency_key,
                              created_at, updated_at
                    """,
                    (refund_no, order_id, reason, amount, key),
                )
                row = cur.fetchone()
            except UniqueViolation as exc:
                conn.rollback()
                pgcode = exc.sqlstate or "23505"
                constraint = None
                if exc.diag is not None:
                    constraint = exc.diag.constraint_name
                if constraint == "ux_refunds_idempotency_key":
                    with connection(read_only=True) as conn2:
                        with conn2.cursor() as cur2:
                            replay = _fetch_by_idempotency(cur2, ctx.user_id, key)
                    if replay:
                        return {
                            "ok": True,
                            "tool": TOOL_NAME,
                            "action": "apply",
                            "idempotent_replay": True,
                            "refund": replay,
                        }
                    raise ToolError("unique_violation", pgcode=pgcode, detail=constraint)
                # 同一订单已有进行中退款：把数据库约束原样暴露为 23505，不改为应用层软检查。
                raise ToolError(
                    "open_refund_exists",
                    pgcode=pgcode,
                    detail=constraint or "ux_refunds_one_open_per_order",
                ) from exc

    refund = {
        "refund_no": row[0],
        "order_no": order_no,
        "status": row[1],
        "reason": row[2],
        "amount_cents": row[3],
        "idempotency_key": row[4],
        "created_at": iso(row[5]),
        "updated_at": iso(row[6]),
    }
    return {
        "ok": True,
        "tool": TOOL_NAME,
        "action": "apply",
        "idempotent_replay": False,
        "refund": refund,
    }


def refund(
    ctx: SessionContext,
    *,
    action: str,
    order_no: str | None = None,
    refund_no: str | None = None,
    reason: str | None = None,
    amount_cents: int | None = None,
    idempotency_key: str | None = None,
) -> dict:
    if action == "query":
        return _query(ctx, order_no=order_no, refund_no=refund_no)
    if action == "apply":
        return _apply(
            ctx,
            order_no=order_no,
            reason=reason,
            amount_cents=amount_cents,
            idempotency_key=idempotency_key,
        )
    raise ToolError("invalid_refund_action")
