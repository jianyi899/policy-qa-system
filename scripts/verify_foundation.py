#!/usr/bin/env python3
"""第一段地基实测验证：数据库连接、表、退款唯一约束、25 篇语料。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
import redis
from dotenv import load_dotenv
from psycopg.errors import UniqueViolation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import check_postgres, check_redis  # noqa: E402

EXPECTED_DOCS = [f"P{i:02d}" for i in range(1, 21)] + [f"M{i:02d}" for i in range(1, 6)]
REQUIRED_TABLES = {
    "users",
    "orders",
    "refunds",
    "documents",
    "document_sections",
    "chunks",
}


def test_refund_constraint(conn: psycopg.Connection) -> dict:
    result = {
        "first_pending_insert": None,
        "second_open_insert_rejected": None,
        "pgcode": None,
        "error_class": None,
        "completed_then_new_pending_allowed": None,
        "reason_and_date_not_in_unique_index": None,
    }
    with conn.cursor() as cur:
        cur.execute("DELETE FROM refunds")
        cur.execute("DELETE FROM orders")
        cur.execute("DELETE FROM users")
        cur.execute(
            """
            INSERT INTO users (external_id, display_name, is_member, membership_tier)
            VALUES ('u-verify-1', '验证用户', TRUE, 'plus')
            RETURNING id
            """
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO orders (
                order_no, user_id, sku, category, shipping_insurance, received_at, order_status
            ) VALUES (
                'SO-VERIFY-1', %s, 'SKU-HX-WATCH-S3', '智能穿戴', TRUE, now(), 'received'
            )
            RETURNING id
            """,
            (user_id,),
        )
        order_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO refunds (refund_no, order_id, status, reason)
            VALUES ('RF-VERIFY-1', %s, 'pending', '理由甲')
            """,
            (order_id,),
        )
        result["first_pending_insert"] = True

        try:
            cur.execute(
                """
                INSERT INTO refunds (refund_no, order_id, status, reason)
                VALUES ('RF-VERIFY-2', %s, 'processing', '理由乙')
                """,
                (order_id,),
            )
            result["second_open_insert_rejected"] = False
        except UniqueViolation as exc:
            result["second_open_insert_rejected"] = True
            result["pgcode"] = exc.sqlstate
            result["error_class"] = type(exc).__name__
            conn.rollback()
            # rollback 会撤掉前面的插入，需要在新事务中重做第一笔后再测完结路径。
            with conn.cursor() as cur2:
                cur2.execute("DELETE FROM refunds")
                cur2.execute("DELETE FROM orders")
                cur2.execute("DELETE FROM users")
                cur2.execute(
                    """
                    INSERT INTO users (external_id, display_name, is_member, membership_tier)
                    VALUES ('u-verify-1', '验证用户', TRUE, 'plus')
                    RETURNING id
                    """
                )
                user_id = cur2.fetchone()[0]
                cur2.execute(
                    """
                    INSERT INTO orders (
                        order_no, user_id, sku, category, shipping_insurance,
                        received_at, order_status
                    ) VALUES (
                        'SO-VERIFY-1', %s, 'SKU-HX-WATCH-S3', '智能穿戴', TRUE, now(), 'received'
                    )
                    RETURNING id
                    """,
                    (user_id,),
                )
                order_id = cur2.fetchone()[0]
                cur2.execute(
                    """
                    INSERT INTO refunds (refund_no, order_id, status, reason)
                    VALUES ('RF-VERIFY-1', %s, 'pending', '理由甲')
                    """,
                    (order_id,),
                )
                cur2.execute(
                    """
                    UPDATE refunds SET status = 'completed', updated_at = now()
                    WHERE refund_no = 'RF-VERIFY-1'
                    """
                )
                cur2.execute(
                    """
                    INSERT INTO refunds (refund_no, order_id, status, reason)
                    VALUES ('RF-VERIFY-3', %s, 'pending', '理由甲')
                    """,
                    (order_id,),
                )
                result["completed_then_new_pending_allowed"] = True
                # 确认唯一索引列只有 order_id，不含 reason、不含日期。
                cur2.execute(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE tablename = 'refunds' AND indexname = 'ux_refunds_one_open_per_order'
                    """
                )
                indexdef = cur2.fetchone()[0]
                result["indexdef"] = indexdef
                result["reason_and_date_not_in_unique_index"] = (
                    "reason" not in indexdef.lower()
                    and "created_at" not in indexdef.lower()
                    and "order_id" in indexdef
                    and "WHERE" in indexdef
                )
            conn.commit()
            return result

        conn.commit()
        return result


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    report: dict = {"ok": True, "checks": {}}

    pg_ok, pg_msg = check_postgres(settings)
    rd_ok, rd_msg = check_redis(settings)
    report["checks"]["postgres"] = {"ok": pg_ok, "detail": pg_msg}
    report["checks"]["redis"] = {"ok": rd_ok, "detail": rd_msg}

    if not pg_ok:
        report["ok"] = False
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                """
            )
            tables = {row[0] for row in cur.fetchall()}
            missing = sorted(REQUIRED_TABLES - tables)
            report["checks"]["tables"] = {
                "ok": not missing,
                "present": sorted(tables),
                "missing": missing,
            }
            cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
            ext = cur.fetchone()
            report["checks"]["pgvector"] = {
                "ok": bool(ext),
                "version": ext[1] if ext else None,
            }
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'users' AND column_name IN
                    ('is_member', 'membership_tier', 'membership_expires_at')
                """
            )
            user_cols = {row[0] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'orders' AND column_name IN
                    ('sku', 'category', 'shipping_insurance', 'received_at', 'user_id')
                """
            )
            order_cols = {row[0] for row in cur.fetchall()}
            report["checks"]["required_columns"] = {
                "ok": user_cols == {
                    "is_member",
                    "membership_tier",
                    "membership_expires_at",
                }
                and order_cols == {
                    "sku",
                    "category",
                    "shipping_insurance",
                    "received_at",
                    "user_id",
                },
                "users": sorted(user_cols),
                "orders": sorted(order_cols),
            }
            cur.execute("SELECT to_regclass('public.chunk_embeddings')")
            embed_table = cur.fetchone()[0]
            report["checks"]["chunk_embeddings"] = {
                "created": embed_table is not None,
                "note": "未锁定 E0 时不应创建该表",
            }
        refund = test_refund_constraint(conn)
        report["checks"]["refund_unique"] = refund
        report["checks"]["refund_unique"]["ok"] = bool(
            refund.get("first_pending_insert")
            and refund.get("second_open_insert_rejected")
            and refund.get("completed_then_new_pending_allowed")
            and refund.get("reason_and_date_not_in_unique_index")
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, title, source_type, scope_type, scope_value, effective_date
                FROM documents
                ORDER BY doc_id
                """
            )
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            report["checks"]["corpus"] = {
                "ok": sorted(ids) == sorted(EXPECTED_DOCS),
                "count": len(ids),
                "ids": ids,
                "missing": [d for d in EXPECTED_DOCS if d not in ids],
                "docs": [
                    {
                        "doc_id": r[0],
                        "title": r[1],
                        "source_type": r[2],
                        "scope_type": r[3],
                        "scope_value": r[4],
                        "effective_date": str(r[5]),
                    }
                    for r in rows
                ],
            }
            cur.execute(
                """
                SELECT doc_id, section_path
                FROM document_sections
                WHERE (doc_id = 'P01' AND section_path LIKE '3.%')
                   OR (doc_id = 'P02' AND section_path LIKE '2.%')
                   OR (doc_id = 'P03' AND section_path LIKE '1.%')
                   OR (doc_id = 'P05' AND section_path LIKE '1.%')
                   OR (doc_id = 'P05' AND section_path LIKE '3.%')
                   OR (doc_id = 'P07' AND section_path LIKE '1.%')
                   OR (doc_id = 'P08' AND section_path LIKE '2.%')
                   OR (doc_id = 'P15' AND section_path LIKE '2.%')
                ORDER BY doc_id, section_path
                """
            )
            eval_rows = cur.fetchall()
            report["checks"]["eval_sections"] = {
                "ok": len(eval_rows) >= 8,
                "rows": [{"doc_id": r[0], "section_path": r[1]} for r in eval_rows],
            }

    for name, check in report["checks"].items():
        if isinstance(check, dict) and "ok" in check and not check["ok"]:
            report["ok"] = False

    if not rd_ok:
        report["ok"] = False

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
