"""调用 DashScope 写入 chunk 向量。不打印、不记录 API 密钥。

E0 已锁定 text-embedding-v4（见 docs/embedding-lock.md，引用 2026-11-18 实测，不重新选型）。
维度从配置 EMBEDDING_DIM 读取，不在代码或建表语句中写死。
"""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings

LOCKED_MODEL = "text-embedding-v4"
DEFAULT_BATCH_SIZE = 10
DEFAULT_MAX_RETRIES = 4

# 仅统计本次进程内真实发出的接口调用，便于启动日志判断是否重复计费。
_api_calls = 0
_api_texts = 0


class EmbeddingError(RuntimeError):
    def __init__(self, error_type: str, message: str | None = None):
        self.error_type = error_type
        super().__init__(error_type)
        self.safe_message = message


def reset_api_counters() -> None:
    global _api_calls, _api_texts
    _api_calls = 0
    _api_texts = 0


def api_usage() -> dict[str, int]:
    return {"embedding_api_calls": _api_calls, "embedding_texts_sent": _api_texts}


def require_api_key(settings: Settings | None = None) -> str:
    if settings is None:
        settings = get_settings()
    key = settings.dashscope_api_key or ""
    if not key:
        raise EmbeddingError("missing_DASHSCOPE_API_KEY")
    return key


def resolve_model_dim(settings: Settings | None = None) -> tuple[str, int]:
    if settings is None:
        settings = get_settings()
    model = (settings.embedding_model or LOCKED_MODEL).strip()
    if model != LOCKED_MODEL:
        raise EmbeddingError("embedding_lock_violation")
    dim = int(settings.embedding_dim)
    if dim <= 0:
        raise EmbeddingError("invalid_EMBEDDING_DIM")
    return model, dim


def embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    expected_dim: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    settings: Settings | None = None,
) -> list[list[float]]:
    """真实调用 DashScope TextEmbedding。失败时抛出 EmbeddingError（不含密钥）。"""
    import time

    global _api_calls, _api_texts

    if settings is None:
        settings = get_settings()
    locked_model, locked_dim = resolve_model_dim(settings)
    model = model or locked_model
    expected_dim = locked_dim if expected_dim is None else int(expected_dim)
    if model != locked_model:
        raise EmbeddingError("embedding_lock_violation")
    if expected_dim != locked_dim:
        raise EmbeddingError("embedding_lock_violation")
    api_key = require_api_key(settings)
    import dashscope
    from dashscope import TextEmbedding

    dashscope.api_key = api_key
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        last_err: EmbeddingError | None = None
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                _api_calls += 1
                _api_texts += len(batch)
                kwargs: dict[str, Any] = {"model": model, "input": batch}
                # 维度从配置传入，便于与建表维度一致；若 SDK 不接受该参数会在下方按返回长度校验。
                try:
                    response = TextEmbedding.call(dimension=expected_dim, **kwargs)
                except TypeError:
                    response = TextEmbedding.call(**kwargs)
            except EmbeddingError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = EmbeddingError(type(exc).__name__)
                time.sleep(min(2 ** attempt, 8))
                continue
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                code = getattr(response, "code", None)
                err_type = str(code) if code else f"http_{status_code}"
                retryable = status_code in {429, 500, 502, 503, 504} or (
                    str(code) in {"Throttling.RateQuota", "Throttling", "ServiceUnavailable"}
                )
                last_err = EmbeddingError(err_type)
                if retryable and attempt + 1 < DEFAULT_MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last_err
            embeddings = []
            output = getattr(response, "output", None) or {}
            if isinstance(output, dict):
                embeddings = output.get("embeddings") or []
            if not embeddings:
                raise EmbeddingError("empty_embeddings")
            ordered: list[list[float] | None] = [None] * len(batch)
            for item in embeddings:
                if not isinstance(item, dict):
                    raise EmbeddingError("unexpected_embedding_item")
                vec = item.get("embedding")
                idx = item.get("text_index")
                if vec is None:
                    raise EmbeddingError("missing_embedding_vector")
                if len(vec) != expected_dim:
                    raise EmbeddingError(f"unexpected_dim_{len(vec)}")
                if idx is None:
                    try:
                        pos = ordered.index(None)
                    except ValueError as exc:  # noqa: BLE001
                        raise EmbeddingError("embedding_index_overflow") from exc
                    ordered[pos] = list(vec)
                else:
                    if not isinstance(idx, int) or idx < 0 or idx >= len(batch):
                        raise EmbeddingError("invalid_text_index")
                    ordered[idx] = list(vec)
            if any(v is None for v in ordered):
                raise EmbeddingError("incomplete_batch_embeddings")
            vectors.extend(ordered)  # type: ignore[arg-type]
            last_err = None
            break
        if last_err is not None:
            raise last_err
        if start + batch_size < len(texts):
            time.sleep(0.15)
    if len(vectors) != len(texts):
        raise EmbeddingError("vector_count_mismatch")
    return vectors
