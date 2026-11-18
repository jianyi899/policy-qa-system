"""根据 users 表字段判断申请当时是否为有效会员。"""

from __future__ import annotations

from datetime import datetime

from app.clock import now_shanghai, to_shanghai


def is_effective_member(
    is_member: bool,
    membership_expires_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not is_member:
        return False
    expires = to_shanghai(membership_expires_at)
    if expires is None:
        return True
    return expires > now_shanghai(now)
