"""FastAPI 应用：健康检查与政策问答入口。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import check_postgres, check_redis
from app.runtime import handle_ask

app = FastAPI(title="policy-qa-system", version="0.6.0")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    use_cascade: bool = True
    use_cache: bool = True


def _public_ask_payload(result: dict) -> dict[str, Any]:
    """对外返回精简字段，不含密钥与完整检索切片。"""
    return {
        "ok": result.get("ok"),
        "question": result.get("question"),
        "answer": result.get("answer"),
        "intent": result.get("intent"),
        "model": result.get("model"),
        "citations": result.get("citations"),
        "cache_hit": result.get("cache_hit"),
        "cacheable": result.get("cacheable"),
        "cache_bypassed": result.get("cache_bypassed"),
        "cascade": result.get("cascade"),
        "usage": result.get("usage"),
        "latency_ms": result.get("latency_ms"),
        "first_token_ms": result.get("first_token_ms"),
        "trace_id": result.get("trace_id"),
        "error_type": result.get("error_type"),
        "tool": result.get("tool"),
        "measured_at": result.get("measured_at"),
        "timezone": result.get("timezone") or "Asia/Shanghai",
    }


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
        "langfuse_base_url": settings.langfuse_url(),
        "qwen_fast_model": settings.qwen_fast_model,
        "qwen_strong_model": settings.qwen_strong_model,
    }


@app.post("/ask")
def ask(body: AskRequest) -> dict:
    result = handle_ask(
        body.question,
        use_cascade=body.use_cascade,
        use_cache=body.use_cache,
    )
    return _public_ask_payload(result)


@app.post("/ask/stream")
def ask_stream(body: AskRequest):
    """SSE：先立刻发出首块（缓存命中时即为完整答案），再补一条 meta。"""

    def events():
        result = handle_ask(
            body.question,
            use_cascade=body.use_cascade,
            use_cache=body.use_cache,
        )
        answer = result.get("answer") or ""
        # 即使答案较短，也先单独发第一字符，便于测量首 token。
        if answer:
            yield f"data: {json.dumps({'token': answer[:1], 'cache_hit': result.get('cache_hit')}, ensure_ascii=False)}\n\n"
            if len(answer) > 1:
                yield f"data: {json.dumps({'token': answer[1:]}, ensure_ascii=False)}\n\n"
        meta = _public_ask_payload(result)
        yield f"data: {json.dumps({'meta': meta}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
