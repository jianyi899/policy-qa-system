#!/usr/bin/env python3
"""第四段可复现种子：冰箱主路径订单（签收约 20 天）及对抗用例所需的其他用户/订单。

可重复执行。只维护 u-stage4-* / SO-STAGE4-* / SO-FRIDGE-* 数据，不删除地基验证样本。
每次运行把冰箱订单的签收时间刷新为「现在往前约 20 天」，以保证主路径口语成立。
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.clock import iso, now_shanghai  # noqa: E402
from app.config import get_settings  # noqa: E402

MEMBER_EXTERNAL_ID = "u-stage4-member"
OTHER_EXTERNAL_ID = "u-stage4-other"
FRIDGE_ORDER_NO = "SO-FRIDGE-20D"
MEMBER_OTHER_ORDER_NO = "SO-STAGE4-MEMBER-OTHER"
OTHER_USER_ORDER_NO = "SO-STAGE4-OTHER-USER"
FRIDGE_SKU = "SKU-HX-FRIDGE-BCD216"
FRIDGE_CATEGORY = "大家电"
FRIDGE_TRACKING = "SF-FRIDGE-20D"


def seed(conn: psycopg.Connection) -> dict:
    now = now_shanghai()
    received = now - timedelta(days=20)
    shipped = received - timedelta(days=3)
    member_expires = now + timedelta(days=180)
    other_received = now - timedelta(days=5)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                external_id, display_name, email, is_member, membership_tier,
                membership_started_at, membership_expires_at, updated_at
            ) VALUES (
                %s, '主路径用户', 'member.stage4@example.invalid', TRUE, 'plus',
                %s, %s, now()
            )
            ON CONFLICT (external_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                is_member = EXCLUDED.is_member,
                membership_tier = EXCLUDED.membership_tier,
                membership_started_at = EXCLUDED.membership_started_at,
                membership_expires_at = EXCLUDED.membership_expires_at,
                updated_at = now()
            RETURNING id
            """,
            (MEMBER_EXTERNAL_ID, now - timedelta(days=400), member_expires),
        )
        member_id = int(cur.fetchone()[0])

        cur.execute(
            """
            INSERT INTO users (
                external_id, display_name, email, is_member, membership_tier,
                membership_started_at, membership_expires_at, updated_at
            ) VALUES (
                %s, '对抗用例其他用户', 'other.stage4@example.invalid', FALSE, 'none',
                NULL, NULL, now()
            )
            ON CONFLICT (external_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                is_member = EXCLUDED.is_member,
                membership_tier = EXCLUDED.membership_tier,
                membership_started_at = EXCLUDED.membership_started_at,
                membership_expires_at = EXCLUDED.membership_expires_at,
                updated_at = now()
            RETURNING id
            """,
            (OTHER_EXTERNAL_ID,),
        )
        other_id = int(cur.fetchone()[0])

        def upsert_order(
            order_no: str,
            user_id: int,
            sku: str,
            category: str,
            insurance: bool,
            received_at,
            amount: int,
        ) -> int:
            cur.execute(
                """
                INSERT INTO orders (
                    order_no, user_id, sku, category, shipping_insurance,
                    received_at, order_status, amount_cents, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'received', %s, now()
                )
                ON CONFLICT (order_no) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    sku = EXCLUDED.sku,
                    category = EXCLUDED.category,
                    shipping_insurance = EXCLUDED.shipping_insurance,
                    received_at = EXCLUDED.received_at,
                    order_status = EXCLUDED.order_status,
                    amount_cents = EXCLUDED.amount_cents,
                    updated_at = now()
                RETURNING id
                """,
                (order_no, user_id, sku, category, insurance, received_at, amount),
            )
            return int(cur.fetchone()[0])

        fridge_id = upsert_order(
            FRIDGE_ORDER_NO, member_id, FRIDGE_SKU, FRIDGE_CATEGORY, True, received, 299900
        )
        member_other_id = upsert_order(
            MEMBER_OTHER_ORDER_NO,
            member_id,
            "SKU-HX-SHIRT-01",
            "服装鞋帽",
            False,
            other_received,
            19900,
        )
        other_order_id = upsert_order(
            OTHER_USER_ORDER_NO,
            other_id,
            "SKU-HX-SNACK-01",
            "食品生鲜",
            False,
            other_received,
            3900,
        )

        # 种子订单上的退款每次重跑清掉，保证对抗用例起点干净。
        cur.execute(
            """
            DELETE FROM refunds
            WHERE order_id IN (
                SELECT id FROM orders
                WHERE order_no IN (%s, %s, %s)
            )
            """,
            (FRIDGE_ORDER_NO, MEMBER_OTHER_ORDER_NO, OTHER_USER_ORDER_NO),
        )

        cur.execute(
            """
            INSERT INTO shipments (
                order_id, tracking_no, carrier, status, shipped_at, delivered_at, updated_at
            ) VALUES (
                %s, %s, '顺丰', 'delivered', %s, %s, now()
            )
            ON CONFLICT (tracking_no) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                carrier = EXCLUDED.carrier,
                status = EXCLUDED.status,
                shipped_at = EXCLUDED.shipped_at,
                delivered_at = EXCLUDED.delivered_at,
                updated_at = now()
            RETURNING id
            """,
            (fridge_id, FRIDGE_TRACKING, shipped, received),
        )
        shipment_id = int(cur.fetchone()[0])
        cur.execute("DELETE FROM shipment_events WHERE shipment_id = %s", (shipment_id,))
        events = [
            (shipped, "武汉仓库", "shipped", "已出库并发运"),
            (shipped + timedelta(days=1), "郑州转运中心", "in_transit", "已到达转运中心"),
            (received, "杭州余杭", "delivered", "已签收，本人妥投"),
        ]
        for event_time, location, status, desc in events:
            cur.execute(
                """
                INSERT INTO shipment_events (shipment_id, event_time, location, status, description)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (shipment_id, event_time, location, status, desc),
            )

    conn.commit()
    return {
        "ok": True,
        "measured_at": iso(now),
        "timezone": "Asia/Shanghai",
        "users": [
            {
                "external_id": MEMBER_EXTERNAL_ID,
                "id": member_id,
                "is_member": True,
                "membership_tier": "plus",
                "membership_expires_at": iso(member_expires),
            },
            {
                "external_id": OTHER_EXTERNAL_ID,
                "id": other_id,
                "is_member": False,
                "membership_tier": "none",
            },
        ],
        "orders": [
            {
                "order_no": FRIDGE_ORDER_NO,
                "id": fridge_id,
                "user_external_id": MEMBER_EXTERNAL_ID,
                "sku": FRIDGE_SKU,
                "category": FRIDGE_CATEGORY,
                "shipping_insurance": True,
                "received_at": iso(received),
                "amount_cents": 299900,
                "purpose": "fridge_main_path",
            },
            {
                "order_no": MEMBER_OTHER_ORDER_NO,
                "id": member_other_id,
                "user_external_id": MEMBER_EXTERNAL_ID,
                "sku": "SKU-HX-SHIRT-01",
                "category": "服装鞋帽",
                "purpose": "same_user_other_order_isolation",
            },
            {
                "order_no": OTHER_USER_ORDER_NO,
                "id": other_order_id,
                "user_external_id": OTHER_EXTERNAL_ID,
                "sku": "SKU-HX-SNACK-01",
                "category": "食品生鲜",
                "purpose": "other_user_isolation",
            },
        ],
        "shipments": [{"tracking_no": FRIDGE_TRACKING, "order_no": FRIDGE_ORDER_NO}],
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    with psycopg.connect(settings.dsn()) as conn:
        report = seed(conn)
    out = ROOT / "reports" / "stage4_seed.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
