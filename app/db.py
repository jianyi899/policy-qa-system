"""数据库与 Redis 连接检查。"""

from __future__ import annotations

import psycopg
import redis

from app.config import Settings


def check_postgres(settings: Settings) -> tuple[bool, str]:
    try:
        with psycopg.connect(settings.dsn(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - 健康检查需要返回原因
        return False, type(exc).__name__


def check_redis(settings: Settings) -> tuple[bool, str]:
    try:
        client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        pong = client.ping()
        client.close()
        return (True, "ok") if pong else (False, "unexpected_pong")
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
