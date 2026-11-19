"""Langfuse Cloud 追踪。宿主为日本区域，不自建。

密钥只从环境变量读取，不得写入日志或仓库。键缺失时所有调用降为 no-op。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.config import get_settings

_client = None
_init_attempted = False
_tracing_enabled: ContextVar[bool] = ContextVar("policyqa_tracing_enabled", default=True)


class _NoopSpan:
    trace_id = None
    id = None

    def update(self, **kwargs) -> None:
        return None

    def end(self) -> None:
        return None


def tracing_is_enabled() -> bool:
    if not _tracing_enabled.get():
        return False
    settings = get_settings()
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def disable_tracing() -> Any:
    return _tracing_enabled.set(False)


def reset_tracing(token: Any) -> None:
    _tracing_enabled.reset(token)


def get_langfuse():
    """返回已配置到日本区域的官方 SDK 客户端；无法初始化时返回 None。"""
    global _client, _init_attempted
    if _client is not None:
        return _client
    if _init_attempted:
        return None
    _init_attempted = True
    settings = get_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    from langfuse import Langfuse

    _client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_url(),
        tracing_enabled=True,
    )
    return _client


def flush() -> None:
    client = get_langfuse()
    if client is not None:
        client.flush()


@contextmanager
def observation(
    *,
    name: str,
    as_type: str = "span",
    input: Any = None,
    output: Any = None,
    metadata: dict | None = None,
    model: str | None = None,
    model_parameters: dict | None = None,
    usage_details: dict | None = None,
) -> Iterator[Any]:
    """创建一个 Langfuse observation；追踪关闭时返回 no-op。"""
    if not tracing_is_enabled():
        yield _NoopSpan()
        return
    client = get_langfuse()
    if client is None:
        yield _NoopSpan()
        return
    kwargs: dict[str, Any] = {
        "name": name,
        "as_type": as_type,
        "input": input,
        "output": output,
        "metadata": metadata,
    }
    if model is not None:
        kwargs["model"] = model
    if model_parameters is not None:
        kwargs["model_parameters"] = model_parameters
    if usage_details is not None:
        kwargs["usage_details"] = usage_details
    with client.start_as_current_observation(**kwargs) as span:
        yield span


def fetch_observations(trace_id: str, *, retries: int = 8, wait_seconds: float = 1.5) -> dict:
    """用官方 API 回读一条 trace 的 observations，确认已写入日本区域。

    使用 observations.get_many（实时读路径）。不返回密钥。
    """
    import time

    client = get_langfuse()
    if client is None:
        return {"ok": False, "error_type": "langfuse_not_configured", "trace_id": trace_id}
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            page = client.api.observations.get_many(
                trace_id=trace_id,
                limit=50,
                fields="core,basic,model,usage,metrics,trace_context",
            )
        except Exception as exc:  # noqa: BLE001
            last_err = type(exc).__name__
            time.sleep(wait_seconds * (attempt + 1))
            continue
        data = getattr(page, "data", None) or []
        items = []
        for obs in data:
            items.append(
                {
                    "id": getattr(obs, "id", None),
                    "trace_id": getattr(obs, "trace_id", None) or getattr(obs, "traceId", None),
                    "name": getattr(obs, "name", None),
                    "type": getattr(obs, "type", None) or getattr(obs, "observation_type", None),
                    "model": getattr(obs, "model", None),
                }
            )
        if items:
            return {
                "ok": True,
                "trace_id": trace_id,
                "n_observations": len(items),
                "observations": items,
                "base_url": get_settings().langfuse_url(),
            }
        last_err = "empty_observations"
        time.sleep(wait_seconds * (attempt + 1))
    return {
        "ok": False,
        "error_type": last_err or "trace_not_found",
        "trace_id": trace_id,
        "base_url": get_settings().langfuse_url(),
    }
