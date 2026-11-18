"""FastAPI 骨架：最小健康检查。完整问答链路不在本阶段实现。"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.db import check_postgres, check_redis

app = FastAPI(title="policy-qa-system", version="0.1.0")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    pg_ok, pg_msg = check_postgres(settings)
    rd_ok, rd_msg = check_redis(settings)
    status = "ok" if pg_ok and rd_ok else "degraded"
    return {
        "status": status,
        "postgres": {"ok": pg_ok, "detail": pg_msg},
        "redis": {"ok": rd_ok, "detail": rd_msg},
        "embedding_model": settings.embedding_model or None,
        "embedding_dim": settings.embedding_dim,
    }
