"""第四段可复现种子：冰箱主路径订单（签收约 20 天）及对抗用例所需的其他用户/订单。

可重复执行。只维护 u-stage4-* / SO-STAGE4-* / SO-FRIDGE-* 数据。
每次运行把冰箱订单的签收时间刷新为「现在往前约 20 天」，以保证主路径口语成立。
不实现退款等写操作，也不写入 shipments 以外的表：物流使用 logistics 表。
"""

from __future__ import annotations

from datetime import timedelta

from app.clock import iso, now_shanghai
from app.config import Settings, get_settings
from app.context import SessionContext
from app.db import connection

MEMBER_EXTERNAL_ID = "u-stage4-member"
OTHER_EXTERNAL_ID = "u-stage4-other"
FRIDGE_ORDER_NO = "SO-FRIDGE-20D"
MEMBER_OTHER_ORDER_NO = "SO-STAGE4-MEMBER-OTHER"
OTHER_USER_ORDER_NO = "SO-STAGE4-OTHER-USER"
FRIDGE_SKU = "SKU-HX-FRIDGE-BCD216"
FRIDGE_CATEGORY = "大家电"
FRIDGE_TRACKING = "SF-FRIDGE-20D"

DEMO_ACCOUNT_CHOICES = [
    {
        "external_id": None,
        "label": "未选择（未登录）",
        "description": "未选演示账号时不能查订单或查物流，对话里自报身份无效。",
    },
    {
        "external_id": MEMBER_EXTERNAL_ID,
        "label": "主路径用户（含冰箱订单 SO-FRIDGE-20D）",
        "description": "签收约 20 天的冰箱订单，用于「质量问题还能退吗」主路径。",
    },
    {
        "external_id": OTHER_EXTERNAL_ID,
        "label": "其他演示用户（无冰箱订单）",
        "description": "用于验证不能查到别人的订单。",
    },
]


def seed(settings: Settings | None = None) -> dict:
    if settings is None:
        settings = get_settings()
    now = now_shanghai()
    received = now - timedelta(days=20)
    shipped = received - timedelta(days=3)
    member_expires = now + timedelta(days=180)
    other_received = now - timedelta(days=5)
    updated = iso(now)

    with connection(settings=settings) as conn:
        conn.execute(
            """
            INSERT INTO users (
                external_id, display_name, email, is_member, membership_tier,
                membership_started_at, membership_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (external_id) DO UPDATE SET
                display_name = excluded.display_name,
                email = excluded.email,
                is_member = excluded.is_member,
                membership_tier = excluded.membership_tier,
                membership_started_at = excluded.membership_started_at,
                membership_expires_at = excluded.membership_expires_at,
                updated_at = excluded.updated_at
            """,
            (
                MEMBER_EXTERNAL_ID,
                "主路径用户",
                "member.stage4@example.invalid",
                1,
                "plus",
                iso(now - timedelta(days=400)),
                iso(member_expires),
                updated,
            ),
        )
        member_id = int(
            conn.execute(
                "SELECT id FROM users WHERE external_id = ?", (MEMBER_EXTERNAL_ID,)
            ).fetchone()[0]
        )

        conn.execute(
            """
            INSERT INTO users (
                external_id, display_name, email, is_member, membership_tier,
                membership_started_at, membership_expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (external_id) DO UPDATE SET
                display_name = excluded.display_name,
                email = excluded.email,
                is_member = excluded.is_member,
                membership_tier = excluded.membership_tier,
                membership_started_at = excluded.membership_started_at,
                membership_expires_at = excluded.membership_expires_at,
                updated_at = excluded.updated_at
            """,
            (
                OTHER_EXTERNAL_ID,
                "对抗用例其他用户",
                "other.stage4@example.invalid",
                0,
                "none",
                None,
                None,
                updated,
            ),
        )
        other_id = int(
            conn.execute(
                "SELECT id FROM users WHERE external_id = ?", (OTHER_EXTERNAL_ID,)
            ).fetchone()[0]
        )

        def upsert_order(
            order_no: str,
            user_id: int,
            sku: str,
            category: str,
            insurance: bool,
            received_at,
            amount: int,
        ) -> int:
            conn.execute(
                """
                INSERT INTO orders (
                    order_no, user_id, sku, category, shipping_insurance,
                    received_at, order_status, amount_cents, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'received', ?, ?)
                ON CONFLICT (order_no) DO UPDATE SET
                    user_id = excluded.user_id,
                    sku = excluded.sku,
                    category = excluded.category,
                    shipping_insurance = excluded.shipping_insurance,
                    received_at = excluded.received_at,
                    order_status = excluded.order_status,
                    amount_cents = excluded.amount_cents,
                    updated_at = excluded.updated_at
                """,
                (
                    order_no,
                    user_id,
                    sku,
                    category,
                    1 if insurance else 0,
                    iso(received_at),
                    amount,
                    updated,
                ),
            )
            return int(
                conn.execute(
                    "SELECT id FROM orders WHERE order_no = ?", (order_no,)
                ).fetchone()[0]
            )

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

        conn.execute(
            """
            INSERT INTO logistics (
                order_id, tracking_no, carrier, status, shipped_at, delivered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tracking_no) DO UPDATE SET
                order_id = excluded.order_id,
                carrier = excluded.carrier,
                status = excluded.status,
                shipped_at = excluded.shipped_at,
                delivered_at = excluded.delivered_at,
                updated_at = excluded.updated_at
            """,
            (fridge_id, FRIDGE_TRACKING, "顺丰", "delivered", iso(shipped), iso(received), updated),
        )

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
                "shipping_insurance": False,
                "received_at": iso(other_received),
                "purpose": "same_user_other_order_isolation",
            },
            {
                "order_no": OTHER_USER_ORDER_NO,
                "id": other_order_id,
                "user_external_id": OTHER_EXTERNAL_ID,
                "sku": "SKU-HX-SNACK-01",
                "category": "食品生鲜",
                "shipping_insurance": False,
                "received_at": iso(other_received),
                "purpose": "other_user_isolation",
            },
        ],
        "logistics": [{"tracking_no": FRIDGE_TRACKING, "order_no": FRIDGE_ORDER_NO}],
    }


def list_demo_accounts(settings: Settings | None = None) -> list[dict]:
    """返回切换器选项。未种子时仍给出固定选项，id 在查库后补上。"""
    if settings is None:
        settings = get_settings()
    by_ext: dict[str, dict] = {}
    try:
        with connection(read_only=True, settings=settings) as conn:
            rows = conn.execute(
                """
                SELECT id, external_id, display_name, is_member, membership_tier
                FROM users
                WHERE external_id IN (?, ?)
                """,
                (MEMBER_EXTERNAL_ID, OTHER_EXTERNAL_ID),
            ).fetchall()
            for row in rows:
                by_ext[str(row["external_id"])] = {
                    "user_id": int(row["id"]),
                    "display_name": row["display_name"],
                    "is_member": bool(row["is_member"]),
                    "membership_tier": row["membership_tier"],
                }
    except FileNotFoundError:
        pass
    out = []
    for choice in DEMO_ACCOUNT_CHOICES:
        item = dict(choice)
        ext = choice["external_id"]
        if ext and ext in by_ext:
            item.update(by_ext[ext])
        out.append(item)
    return out


def context_from_demo_account(
    external_id: str | None,
    *,
    settings: Settings | None = None,
) -> SessionContext | None:
    """只接受已种子的演示账号。空值表示未登录。未知账号拒绝，不得凭对话里的身份创建会话。"""
    if external_id is None or str(external_id).strip() in {"", "none", "未选择（未登录）"}:
        return None
    ext = str(external_id).strip()
    allowed = {c["external_id"] for c in DEMO_ACCOUNT_CHOICES if c["external_id"]}
    if ext not in allowed:
        raise ValueError("unknown_demo_account")
    if settings is None:
        settings = get_settings()
    with connection(read_only=True, settings=settings) as conn:
        row = conn.execute(
            "SELECT id, external_id, display_name FROM users WHERE external_id = ?",
            (ext,),
        ).fetchone()
    if not row:
        raise ValueError("demo_account_not_seeded")
    return SessionContext(user_id=int(row["id"]), external_id=row["external_id"], display_name=row["display_name"])
