#!/usr/bin/env python3
"""第四段对抗用例实测：错误路径不得产生错误副作用。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.clock import iso, now_shanghai  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import MissingSessionContext, SessionContext  # noqa: E402
from app.db import connection  # noqa: E402
from app.tools import TOOL_SCHEMAS, invoke  # noqa: E402
from app.tools.errors import ToolError  # noqa: E402
from scripts.seed_stage4 import (  # noqa: E402
    FRIDGE_ORDER_NO,
    MEMBER_EXTERNAL_ID,
    MEMBER_OTHER_ORDER_NO,
    OTHER_EXTERNAL_ID,
    OTHER_USER_ORDER_NO,
    seed,
)

FINGERPRINT_SQL = {
    "users": "SELECT id, external_id, is_member, membership_tier, membership_expires_at FROM users ORDER BY id",
    "orders": "SELECT id, order_no, user_id, sku, category, shipping_insurance, received_at, order_status, amount_cents FROM orders ORDER BY id",
    "refunds": "SELECT id, refund_no, order_id, status, reason, amount_cents, idempotency_key FROM refunds ORDER BY id",
    "shipments": "SELECT id, order_id, tracking_no, carrier, status, shipped_at, delivered_at FROM shipments ORDER BY id",
    "shipment_events": "SELECT id, shipment_id, event_time, location, status, description FROM shipment_events ORDER BY id",
    "daily_reports": "SELECT report_date, generated_at, orders_received, refunds_created, refunds_open, refunds_completed, payload FROM daily_reports ORDER BY report_date",
}


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value).hex()
    return value


def fingerprint(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for name, sql in FINGERPRINT_SQL.items():
            cur.execute(sql)
            cols = [d.name for d in cur.description]
            rows = []
            for row in cur.fetchall():
                rows.append({cols[i]: _jsonable(row[i]) for i in range(len(cols))})
            out[name] = rows
    return out


def refunds_for_order(conn, order_no: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.refund_no, r.order_id, r.status, r.reason, r.idempotency_key
            FROM refunds r
            JOIN orders o ON o.id = r.order_id
            WHERE o.order_no = %s
            ORDER BY r.id
            """,
            (order_no,),
        )
        cols = [d.name for d in cur.description]
        return [{cols[i]: _jsonable(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def load_user(conn, external_id: str) -> SessionContext:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, external_id, display_name FROM users WHERE external_id = %s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"missing_user_{external_id}")
    return SessionContext(user_id=int(row[0]), external_id=row[1], display_name=row[2])


def case_result(name: str, ok: bool, **kwargs) -> dict:
    data = {"name": name, "ok": ok}
    data.update(kwargs)
    return data


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    cases: list[dict] = []

    with psycopg.connect(settings.dsn()) as conn:
        seed(conn)
        member = load_user(conn, MEMBER_EXTERNAL_ID)
        other = load_user(conn, OTHER_EXTERNAL_ID)

        # --- 1) 查询类工具不得误写数据 ---
        before = fingerprint(conn)
        query_errors = []
        try:
            invoke("query_order", {"keyword": "冰箱"}, member)
            invoke("query_logistics", {"order_no": FRIDGE_ORDER_NO}, member)
            invoke("refund", {"action": "query", "order_no": FRIDGE_ORDER_NO}, member)
            invoke("daily_report", {"action": "query"}, member)
        except Exception as exc:  # noqa: BLE001
            query_errors.append(getattr(exc, "error_type", None) or type(exc).__name__)
        after_query = fingerprint(conn)
        cases.append(
            case_result(
                "query_tools_do_not_write",
                ok=(not query_errors and before == after_query),
                expected="query_order / query_logistics / refund(query) / daily_report(query) 前后库快照一致",
                query_errors=query_errors,
                changed_tables=[k for k in before if before[k] != after_query[k]],
            )
        )

        # --- 2) 缺会话上下文时不得用参数里的 user_id 顶替，且不得写库 ---
        before = fingerprint(conn)
        missing_ok = True
        details = []
        attempts = [
            ("query_order", {"keyword": "冰箱", "user_id": member.user_id}),
            ("refund", {"action": "apply", "order_no": FRIDGE_ORDER_NO, "reason": "质量问题", "user_id": member.user_id, "idempotency_key": "should-not-apply"}),
            ("query_logistics", {"order_no": FRIDGE_ORDER_NO, "user_id": member.user_id}),
            ("daily_report", {"action": "generate", "user_id": member.user_id}),
        ]
        for name, args in attempts:
            try:
                invoke(name, args, None)
                missing_ok = False
                details.append({"tool": name, "error": "expected_missing_session_context_but_succeeded"})
            except MissingSessionContext:
                details.append({"tool": name, "error": "missing_session_context"})
            except Exception as exc:  # noqa: BLE001
                missing_ok = False
                details.append({"tool": name, "error": getattr(exc, "error_type", None) or type(exc).__name__})
        schema_has_user_id = False
        for schema in TOOL_SCHEMAS:
            props = (schema.get("function") or {}).get("parameters", {}).get("properties", {})
            if any(k.lower() in {"user_id", "userid", "uid"} for k in props):
                schema_has_user_id = True
        after_missing = fingerprint(conn)
        cases.append(
            case_result(
                "missing_session_context_rejects_user_id_argument",
                ok=missing_ok and after_missing == before and not schema_has_user_id,
                expected="无 SessionContext 时即使参数带 user_id 也拒绝，且工具签名不含 user_id，库无写入",
                attempts=details,
                schema_has_user_id=schema_has_user_id,
                wrote=after_missing != before,
            )
        )

        # --- 3) 合法申请一笔进行中退款 ---
        apply1 = invoke(
            "refund",
            {
                "action": "apply",
                "order_no": FRIDGE_ORDER_NO,
                "reason": "质量问题-压缩机制冷异常",
                "idempotency_key": "idem-fridge-quality-1",
            },
            member,
        )
        first_refund = apply1.get("refund") or {}
        first_open = refunds_for_order(conn, FRIDGE_ORDER_NO)
        cases.append(
            case_result(
                "first_apply_creates_one_pending",
                ok=bool(
                    apply1.get("ok")
                    and not apply1.get("idempotent_replay")
                    and first_refund.get("status") == "pending"
                    and len(first_open) == 1
                ),
                expected="首次申请成功，订单上只有一笔 pending 退款",
                result={"refund_no": first_refund.get("refund_no"), "status": first_refund.get("status"), "n": len(first_open)},
            )
        )

        # --- 4) 同一订单、不同幂等键再次申请：应被库约束拒绝，错误码 23505 ---
        before_dup = refunds_for_order(conn, FRIDGE_ORDER_NO)
        other_order_before = refunds_for_order(conn, MEMBER_OTHER_ORDER_NO)
        other_user_before = refunds_for_order(conn, OTHER_USER_ORDER_NO)
        dup_error = None
        try:
            invoke(
                "refund",
                {
                    "action": "apply",
                    "order_no": FRIDGE_ORDER_NO,
                    "reason": "另一条理由-不要据此新建",
                    "idempotency_key": "idem-fridge-quality-2",
                },
                member,
            )
            dup_ok = False
            dup_error = {"error_type": "second_apply_unexpectedly_succeeded"}
        except ToolError as exc:
            dup_ok = exc.error_type == "open_refund_exists" and exc.pgcode == "23505"
            dup_error = {"error_type": exc.error_type, "pgcode": exc.pgcode, "detail": exc.detail}
        after_dup = refunds_for_order(conn, FRIDGE_ORDER_NO)
        cases.append(
            case_result(
                "duplicate_open_refund_rejected_by_unique_index",
                ok=bool(dup_ok and after_dup == before_dup and len(after_dup) == 1),
                expected="不同幂等键再次申请进行中退款被 ux_refunds_one_open_per_order 拒绝，pgcode=23505，不新增行",
                error=dup_error,
                n_refunds=len(after_dup),
            )
        )

        # --- 5) 同一幂等键重试：返回原单，不创建第二笔 ---
        replay = invoke(
            "refund",
            {
                "action": "apply",
                "order_no": FRIDGE_ORDER_NO,
                "reason": "重试时即使理由不同也不应写入新键",
                "idempotency_key": "idem-fridge-quality-1",
            },
            member,
        )
        after_replay = refunds_for_order(conn, FRIDGE_ORDER_NO)
        cases.append(
            case_result(
                "idempotent_retry_does_not_create_second_refund",
                ok=bool(
                    replay.get("ok")
                    and replay.get("idempotent_replay") is True
                    and (replay.get("refund") or {}).get("refund_no") == first_refund.get("refund_no")
                    and after_replay == before_dup
                    and len(after_replay) == 1
                ),
                expected="相同幂等键重试返回原退款单，不新增记录；幂等键不含当日或理由",
                replay={
                    "idempotent_replay": replay.get("idempotent_replay"),
                    "refund_no": (replay.get("refund") or {}).get("refund_no"),
                    "idempotency_key": (replay.get("refund") or {}).get("idempotency_key"),
                },
                n_refunds=len(after_replay),
            )
        )

        # --- 6) 不能动到其他订单或其他用户的退款；参数里的 user_id 不能越权 ---
        isolation_errors = []
        # 当前用户带上其他用户 id，仍只能看到自己的冰箱订单，不能改别人的单。
        leaked = invoke("query_order", {"order_no": OTHER_USER_ORDER_NO, "user_id": other.user_id}, member)
        if leaked.get("n") != 0 or leaked.get("orders"):
            isolation_errors.append("member_query_other_user_order_leaked")
        try:
            invoke(
                "refund",
                {
                    "action": "apply",
                    "order_no": OTHER_USER_ORDER_NO,
                    "reason": "越权申请",
                    "idempotency_key": "idem-should-not-exist",
                    "user_id": other.user_id,
                },
                member,
            )
            isolation_errors.append("member_apply_other_user_order_succeeded")
        except ToolError as exc:
            if exc.error_type != "order_not_found":
                isolation_errors.append(f"member_apply_other_unexpected_{exc.error_type}")

        try:
            invoke(
                "refund",
                {
                    "action": "apply",
                    "order_no": FRIDGE_ORDER_NO,
                    "reason": "其他用户试图申请",
                    "idempotency_key": "idem-other-user-attack",
                },
                other,
            )
            isolation_errors.append("other_user_apply_fridge_succeeded")
        except ToolError as exc:
            if exc.error_type != "order_not_found":
                isolation_errors.append(f"other_user_apply_unexpected_{exc.error_type}")

        other_query = invoke("query_order", {"order_no": FRIDGE_ORDER_NO}, other)
        if other_query.get("n") != 0 or other_query.get("orders"):
            isolation_errors.append("other_user_query_fridge_leaked")

        other_refund_query = invoke("refund", {"action": "query", "order_no": FRIDGE_ORDER_NO}, other)
        if other_refund_query.get("n") != 0 or other_refund_query.get("refunds"):
            isolation_errors.append("other_user_query_fridge_refund_leaked")

        # 已有进行中退款的冰箱单，再对同一用户的另一订单申请，应能成功（互不影响）。
        other_order_apply = invoke(
            "refund",
            {
                "action": "apply",
                "order_no": MEMBER_OTHER_ORDER_NO,
                "reason": "尺码不合适",
                "idempotency_key": "idem-member-other-order",
            },
            member,
        )
        fridge_after_iso = refunds_for_order(conn, FRIDGE_ORDER_NO)
        other_order_after = refunds_for_order(conn, MEMBER_OTHER_ORDER_NO)
        other_user_after = refunds_for_order(conn, OTHER_USER_ORDER_NO)
        isolation_ok = (
            not isolation_errors
            and fridge_after_iso == after_replay
            and other_user_after == other_user_before
            and len(other_order_before) == 0
            and len(other_order_after) == 1
            and other_order_apply.get("ok")
        )
        cases.append(
            case_result(
                "cannot_touch_other_user_or_other_order_refunds",
                ok=isolation_ok,
                expected="不能查询或申请他人订单退款；参数中的 user_id 无效；他人退款不变；本用户另一订单的新退款不影响冰箱单原退款",
                isolation_errors=isolation_errors,
                fridge_refunds=fridge_after_iso,
                member_other_order_refunds=other_order_after,
                other_user_refunds=other_user_after,
            )
        )

        # 工具签名确认
        cases.append(
            case_result(
                "tool_signatures_omit_user_id",
                ok=not schema_has_user_id,
                expected="四个工具的 JSON Schema 参数均不含 user_id",
                tools=[s["function"]["name"] for s in TOOL_SCHEMAS],
            )
        )

    report = {
        "ok": all(c["ok"] for c in cases),
        "measured_at": iso(now_shanghai()),
        "timezone": "Asia/Shanghai",
        "n_cases": len(cases),
        "n_passed": sum(1 for c in cases if c["ok"]),
        "n_failed": sum(1 for c in cases if not c["ok"]),
        "cases": cases,
        "no_incorrect_side_effects": all(c["ok"] for c in cases),
    }
    out = ROOT / "reports" / "stage4_adversarial.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
