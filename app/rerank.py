"""调用 DashScope gte-rerank API。不在本地跑 reranker，不打印密钥。"""

from __future__ import annotations

import os
import time

DEFAULT_MODEL = "gte-rerank"
FALLBACK_MODEL = "gte-rerank-v2"
DEFAULT_MAX_RETRIES = 4


class RerankError(RuntimeError):
    def __init__(self, error_type: str, message: str | None = None):
        self.error_type = error_type
        super().__init__(error_type)
        self.safe_message = message


def require_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY") or ""
    if not key:
        raise RerankError("missing_DASHSCOPE_API_KEY")
    return key


def _is_retryable(status_code, code) -> bool:
    return status_code in {429, 500, 502, 503, 504} or (
        str(code) in {"Throttling.RateQuota", "Throttling", "ServiceUnavailable"}
    )


def _is_model_unavailable(status_code, code, message: str | None) -> bool:
    text = " ".join(str(x) for x in (code, message) if x)
    lowered = text.lower()
    tokens = (
        "model_not_found",
        "model not found",
        "invalidmodel",
        "invalid_model",
        "model_not_exist",
        "notexist",
        "not exist",
        "unavailable",
        "discontinued",
        "下线",
        "不存在",
        "已停用",
        "accessdenied.unpurchased",
        "arrearage",
    )
    if any(tok in lowered or tok in text for tok in tokens):
        return True
    # 部分下线模型会返回 404 / 400 并带 model 相关 code
    if status_code in {400, 404} and "model" in lowered:
        return True
    return False


def _parse_results(response) -> list[dict]:
    output = getattr(response, "output", None)
    results = None
    if output is None:
        raise RerankError("empty_rerank_output")
    if hasattr(output, "results"):
        results = output.results
    elif isinstance(output, dict):
        results = output.get("results")
    if not results:
        raise RerankError("empty_rerank_results")
    parsed: list[dict] = []
    for item in results:
        if isinstance(item, dict):
            idx = item.get("index")
            score = item.get("relevance_score")
        else:
            idx = getattr(item, "index", None)
            score = getattr(item, "relevance_score", None)
        if not isinstance(idx, int):
            raise RerankError("invalid_rerank_index")
        parsed.append(
            {
                "index": idx,
                "relevance_score": float(score) if score is not None else None,
            }
        )
    return parsed


def rerank_documents(
    query: str,
    documents: list[str],
    *,
    model: str = DEFAULT_MODEL,
    top_n: int | None = None,
    allow_fallback: bool = True,
) -> dict:
    """对 documents 重排，返回按新顺序的下标与所用模型。

    优先使用给定 model（默认 gte-rerank）。若该模型不可用且 allow_fallback，
    再尝试 gte-rerank-v2，并在结果中标明发生了回退。失败只暴露 error_type。
    """
    if not documents:
        return {"model": model, "fallback_from": None, "results": []}
    api_key = require_api_key()
    import dashscope
    from dashscope import TextReRank

    dashscope.api_key = api_key
    models_to_try = [model]
    if allow_fallback and model != FALLBACK_MODEL:
        models_to_try.append(FALLBACK_MODEL)

    last_err: RerankError | None = None
    used_fallback_from = None
    for idx, current_model in enumerate(models_to_try):
        last_err = None
        for attempt in range(DEFAULT_MAX_RETRIES):
            try:
                response = TextReRank.call(
                    model=current_model,
                    query=query,
                    documents=documents,
                    top_n=top_n if top_n is not None else len(documents),
                    return_documents=False,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = RerankError(type(exc).__name__)
                time.sleep(min(2 ** attempt, 8))
                continue
            status_code = getattr(response, "status_code", None)
            code = getattr(response, "code", None)
            message = getattr(response, "message", None)
            if status_code != 200:
                err_type = str(code) if code else f"http_{status_code}"
                last_err = RerankError(err_type)
                if _is_model_unavailable(status_code, code, message):
                    # 换下一个模型，不再对该模型重试
                    break
                if _is_retryable(status_code, code) and attempt + 1 < DEFAULT_MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise last_err
            results = _parse_results(response)
            # 校验下标合法
            n = len(documents)
            seen = set()
            for item in results:
                if item["index"] < 0 or item["index"] >= n or item["index"] in seen:
                    raise RerankError("invalid_rerank_index")
                seen.add(item["index"])
            if idx > 0:
                used_fallback_from = models_to_try[0]
            return {
                "model": current_model,
                "fallback_from": used_fallback_from,
                "results": results,
            }
        # 当前模型不可用，尝试回退
        if last_err is not None and idx + 1 < len(models_to_try):
            continue
        if last_err is not None:
            raise last_err
    if last_err is not None:
        raise last_err
    raise RerankError("rerank_failed")
