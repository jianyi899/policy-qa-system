"""调用 DashScope 写入 chunk 向量。不打印、不记录 API 密钥。

E0 已锁定：text-embedding-v4，维度以实测 1024 为准，禁止更换模型或维度。
"""

from __future__ import annotations

import os
import time

LOCKED_MODEL = "text-embedding-v4"
LOCKED_DIM = 1024
DEFAULT_BATCH_SIZE = 10
DEFAULT_MAX_RETRIES = 4


class EmbeddingError(RuntimeError):
    def __init__(self, error_type: str, message: str | None = None):
        self.error_type = error_type
        # message 仅用于内部判断，对外报告只用 error_type，避免带出密钥或请求细节。
        super().__init__(error_type)
        self.safe_message = message


def require_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY") or ""
    if not key:
        raise EmbeddingError("missing_DASHSCOPE_API_KEY")
    return key


def embed_texts(
    texts: list[str],
    *,
    model: str = LOCKED_MODEL,
    expected_dim: int = LOCKED_DIM,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """真实调用 DashScope TextEmbedding。失败时抛出 EmbeddingError（不含密钥）。"""
    if model != LOCKED_MODEL or expected_dim != LOCKED_DIM:
        raise EmbeddingError("embedding_lock_violation")
    api_key = require_api_key()
    import dashscope
    from dashscope import TextEmbedding

    dashscope.api_key = api_key
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        last_err: EmbeddingError | None = None
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                response = TextEmbedding.call(model=model, input=batch)
            except Exception as exc:  # noqa: BLE001
                last_err = EmbeddingError(type(exc).__name__)
                time.sleep(min(2 ** attempt, 8))
                continue
            status_code = getattr(response, "status_code", None)
            if status_code != 200:
                code = getattr(response, "code", None)
                err_type = str(code) if code else f"http_{status_code}"
                # 对限流重试，其他错误直接失败。
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
            # DashScope 可能按 text_index 返回，需按原顺序对齐。
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
                    # 无下标则按返回顺序填入剩余空位
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
        # 轻微间隔，降低触发限流的可能
        if start + batch_size < len(texts):
            time.sleep(0.15)
    if len(vectors) != len(texts):
        raise EmbeddingError("vector_count_mismatch")
    return vectors
