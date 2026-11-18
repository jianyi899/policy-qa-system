"""日报工具。query 只读实时聚合（及已有快照），generate 才按自然日写入快照。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from app.clock import SHANGHAI, iso, now_shanghai, parse_report_date
from app.context import SessionContext
from app.db import connection

TOOL_NAME = "daily_report"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "查询或生成运营日报。query 为只读，统计当日签收订单与退款情况，不写库；"
            "generate 按 Asia/Shanghai 自然日幂等写入快照，不修改订单或退款。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["query", "generate"],
                    "description": "query 只读查询，generate 写入或更新当日快照。",
                },
                "report_date": {
                    "type": "string",
                    "description": "日报日期，格式 YYYY-MM-DD，默认今天（Asia/Shanghai）。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _day_bounds(report_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(report_date, time.min, tzinfo=SHANGHAI)
    end = start + timedelta(days=1)
    return start, end


def _load_snapshot(cur, report_date: date) -> dict | None:
    cur.execute(
        """
        SELECT report_date, generated_at, orders_received, refunds_created,
               refunds_open, refunds_completed, payload
        FROM daily_reports
        WHERE report_date = %s
        """,
        (report_date,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "report_date": row[0].isoformat(),
        "generated_at": iso(row[1]),
        "orders_received": row[2],
        "refunds_created": row[3],
        "refunds_open": row[4],
        "refunds_completed": row[5],
        "payload": row[6],
    }


def _compute_live(cur, report_date: date) -> dict:
    start, end = _day_bounds(report_date)
    cur.execute(
        """
        SELECT COUNT(*) FROM orders
        WHERE received_at >= %s AND received_at < %s
        """,
        (start, end),
    )
    orders_received = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT COUNT(*) FROM refunds
        WHERE created_at >= %s AND created_at < %s
        """,
        (start, end),
    )
    refunds_created = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT COUNT(*) FROM refunds
        WHERE status IN ('pending', 'processing')
        """
    )
    refunds_open = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT COUNT(*) FROM refunds
        WHERE status = 'completed'
          AND updated_at >= %s AND updated_at < %s
        """,
        (start, end),
    )
    refunds_completed = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT status, COUNT(*) FROM refunds
        GROUP BY status
        ORDER BY status
        """
    )
    status_counts = {row[0]: int(row[1]) for row in cur.fetchall()}
    return {
        "report_date": report_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "computed_at": iso(now_shanghai()),
        "orders_received": orders_received,
        "refunds_created": refunds_created,
        "refunds_open": refunds_open,
        "refunds_completed": refunds_completed,
        "payload": {"refund_status_counts": status_counts},
    }


def daily_report(
    ctx: SessionContext,
    *,
    action: str,
    report_date: str | None = None,
) -> dict:
    # ctx 必须存在（由调度层检查），但日报聚合不按用户过滤，也绝不根据工具参数接收 user_id。
    _ = ctx
    day = parse_report_date(report_date)

    if action == "query":
        with connection(read_only=True) as conn:
            with conn.cursor() as cur:
                live = _compute_live(cur, day)
                snapshot = _load_snapshot(cur, day)
        return {
            "ok": True,
            "tool": TOOL_NAME,
            "action": "query",
            "live": live,
            "snapshot": snapshot,
            "wrote": False,
        }

    if action == "generate":
        with connection(read_only=False) as conn:
            with conn.cursor() as cur:
                live = _compute_live(cur, day)
                cur.execute(
                    """
                    INSERT INTO daily_reports (
                        report_date, generated_at, orders_received, refunds_created,
                        refunds_open, refunds_completed, payload
                    ) VALUES (
                        %s, now(), %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (report_date) DO UPDATE SET
                        generated_at = EXCLUDED.generated_at,
                        orders_received = EXCLUDED.orders_received,
                        refunds_created = EXCLUDED.refunds_created,
                        refunds_open = EXCLUDED.refunds_open,
                        refunds_completed = EXCLUDED.refunds_completed,
                        payload = EXCLUDED.payload
                    """,
                    (
                        day,
                        live["orders_received"],
                        live["refunds_created"],
                        live["refunds_open"],
                        live["refunds_completed"],
                        Json(live["payload"]),
                    ),
                )
                snapshot = _load_snapshot(cur, day)
        return {
            "ok": True,
            "tool": TOOL_NAME,
            "action": "generate",
            "live": live,
            "snapshot": snapshot,
            "wrote": True,
        }

    from app.tools.errors import ToolError

    raise ToolError("invalid_daily_report_action")
