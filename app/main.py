"""FastAPI 应用。第一段提供健康检查；问答主路径在后续阶段接入。"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.db import check_sqlite

app = FastAPI(title="policy-qa-system", version="1.0.0-lite")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    sqlite = check_sqlite(settings)
    status = "ok" if sqlite.get("ok") else "degraded"
    return {
        "status": status,
        "sqlite": sqlite,
        "embedding_model": settings.embedding_model or None,
        "embedding_dim": settings.embedding_dim,
        "qwen_fast_model": settings.qwen_fast_model,
        "qwen_strong_model": settings.qwen_strong_model,
        "policy_qa_home": str(settings.home_dir()),
    }
