"""统一使用 Asia/Shanghai 处理业务日与签收时限。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(SHANGHAI)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI)
    return now.astimezone(SHANGHAI)


def to_shanghai(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI)


def parse_datetime(value) -> datetime | None:
    """把数据库里的 ISO 文本或 datetime 解析为上海时区。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_shanghai(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return to_shanghai(datetime.fromisoformat(text))


def iso(dt: datetime | None) -> str | None:
    converted = to_shanghai(dt)
    return converted.isoformat() if converted else None


def today(now: datetime | None = None) -> date:
    return now_shanghai(now).date()


def parse_report_date(value: str | date | None, *, now: datetime | None = None) -> date:
    if value is None or value == "":
        return today(now)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    return date.fromisoformat(text[:10])


def calendar_days_since(start: datetime, *, now: datetime | None = None) -> int:
    """签收当日起算的日历天数差。签收当日为 0。"""
    start_d = to_shanghai(start)
    if start_d is None:
        raise ValueError("missing_start")
    return (today(now) - start_d.date()).days


def no_reason_deadline(received_at: datetime) -> datetime:
    """第 7 日 24:00，即签收日后第 7 个日历日结束（次日 00:00）。"""
    rec = to_shanghai(received_at)
    if rec is None:
        raise ValueError("missing_received_at")
    return datetime.combine(rec.date() + timedelta(days=7), time.min, tzinfo=SHANGHAI)


def within_7_day_no_reason(received_at: datetime, *, now: datetime | None = None) -> bool:
    return now_shanghai(now) < no_reason_deadline(received_at)


def add_months(dt: datetime, months: int) -> datetime:
    rec = to_shanghai(dt)
    if rec is None:
        raise ValueError("missing_datetime")
    year = rec.year + (rec.month - 1 + months) // 12
    month = (rec.month - 1 + months) % 12 + 1
    day = rec.day
    # 简单处理月末：若目标月没有该日，则退到该月最后一天。
    for candidate in range(day, 0, -1):
        try:
            return rec.replace(year=year, month=month, day=candidate)
        except ValueError:
            continue
    raise ValueError("invalid_month_shift")


def within_warranty_months(received_at: datetime, months: int = 12, *, now: datetime | None = None) -> bool:
    return now_shanghai(now) < add_months(received_at, months)
