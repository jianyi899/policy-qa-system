"""FastAPI 应用：健康检查与问答入口。演示账号只能从已知列表选取，不能传 user_id。单轮接口；多轮请用页面会话。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import check_sqlite
from app.orchestrator import handle_question
from app.seed import context_from_demo_account, list_demo_accounts

app = FastAPI(title="policy-qa-system", version="1.0.0-lite")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    # 只接受演示账号切换器中的 external_id，例如 u-stage4-member；空表示未登录。
    # 不接受 user_id。
    demo_account: str | None = None


def _public_ask_payload(result: dict) -> dict[str, Any]:
    """对外返回精简字段，不含密钥与完整检索切片。"""
    return {
        "ok": result.get("ok"),
        "question": result.get("question"),
        "answer": result.get("answer"),
        "intent": result.get("intent"),
        "model": result.get("model"),
        "cascaded": result.get("cascaded"),
        "cascade": result.get("cascade"),
        "order_fields_injected": result.get("order_fields_injected"),
        "cited_policies": result.get("cited_policies"),
        "citations": result.get("citations"),
        "order_reused": result.get("order_reused"),
        "tool": result.get("tool"),
        "error_type": result.get("error_type"),
        "usage": result.get("usage"),
        "elapsed_ms": result.get("elapsed_ms"),
        "measured_at": result.get("measured_at"),
        "timezone": result.get("timezone") or "Asia/Shanghai",
        "session": result.get("session"),
        "meets_expected": result.get("meets_expected"),
    }


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


@app.get("/demo-accounts")
def demo_accounts() -> dict:
    return {"accounts": list_demo_accounts()}


@app.post("/ask")
def ask(body: AskRequest) -> dict:
    try:
        ctx = context_from_demo_account(body.demo_account)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = handle_question(body.question, ctx)
    return _public_ask_payload(result)
