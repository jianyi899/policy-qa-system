"""数据库与 Redis 连接检查，以及工具使用的连接助手。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import redis
from psycopg import Connection

from app.config import Settings, get_settings


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


def connect(settings: Settings | None = None, *, read_only: bool = False) -> Connection:
    """打开一条连接。调用方负责关闭；只读连接在会话级禁止写入。"""
    if settings is None:
        settings = get_settings()
    conn = psycopg.connect(settings.dsn())
    if read_only:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


@contextmanager
def connection(*, read_only: bool = False, settings: Settings | None = None) -> Iterator[Connection]:
    conn = connect(settings, read_only=read_only)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
