"""政策问答与检索结果的 Redis 缓存。

只覆盖纯政策问答和检索结果。工具类不得调用本模块。
缓存键按规范化后的精确文本计算，同义变体不得命中。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

import redis

from app.config import get_settings
from app.embeddings import LOCKED_DIM, LOCKED_MODEL
from app.pipeline import load_locked_pipeline

ANSWER_KIND = "answer"
RETRIEVAL_KIND = "retrieval"


def normalize_text(text: str) -> str:
    """仅做 Unicode 规范化与空白折叠，不做同义改写。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )


def _ns() -> str:
    return get_settings().cache_namespace


def _pipeline_tag() -> str:
    pipe = load_locked_pipeline()
    return (
        f"{pipe.get('chunking_name')}|{pipe.get('retriever') or pipe.get('retrieval')}|"
        f"{LOCKED_MODEL}|{LOCKED_DIM}|rrf60"
    )


def _key(kind: str, material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_ns()}:{kind}:{digest}"


def answer_key(question: str, *, model_family: str = "cascade") -> str:
    material = f"answer|{_pipeline_tag()}|{model_family}|{normalize_text(question)}"
    return _key(ANSWER_KIND, material)


def retrieval_key(query: str, *, top_k: int) -> str:
    material = f"retrieval|{_pipeline_tag()}|{int(top_k)}|{normalize_text(query)}"
    return _key(RETRIEVAL_KIND, material)


def get_json(key: str) -> dict | None:
    try:
        raw = _client().get(key)
    except redis.RedisError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def set_json(key: str, payload: dict, *, ttl_seconds: int | None = None) -> bool:
    settings = get_settings()
    ttl = int(settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        _client().set(key, body, ex=ttl)
        return True
    except redis.RedisError:
        return False


def delete_keys(*keys: str) -> int:
    if not keys:
        return 0
    try:
        return int(_client().delete(*keys))
    except redis.RedisError:
        return 0


def flush_namespace() -> int:
    """只删除本应用命名空间下的键，不影响 Redis 里的其他数据。"""
    client = _client()
    pattern = f"{_ns()}:*"
    deleted = 0
    try:
        for key in client.scan_iter(match=pattern, count=200):
            deleted += int(client.delete(key))
    except redis.RedisError:
        return deleted
    return deleted


def exists(key: str) -> bool:
    try:
        return bool(_client().exists(key))
    except redis.RedisError:
        return False


def snapshot_namespace() -> set[str]:
    client = _client()
    keys: set[str] = set()
    try:
        for key in client.scan_iter(match=f"{_ns()}:*", count=200):
            keys.add(str(key))
    except redis.RedisError:
        return keys
    return keys


def compact_answer_payload(result: dict) -> dict:
    """写入问答缓存的精简字段。不含密钥，也不放完整检索命中列表。"""
    return {
        "ok": bool(result.get("ok")),
        "question": result.get("question"),
        "answer": result.get("answer"),
        "model": result.get("model"),
        "citations": result.get("citations") or [],
        "citation_score": result.get("citation_score"),
        "usage": result.get("usage"),
        "conflict_resolution": {
            "rule": (result.get("conflict_resolution") or {}).get("rule"),
            "explanation": (result.get("conflict_resolution") or {}).get("explanation"),
            "has_finer_scope": (result.get("conflict_resolution") or {}).get("has_finer_scope"),
            "by_scope_doc_ids": (result.get("conflict_resolution") or {}).get("by_scope_doc_ids"),
            "n_applicable": (result.get("conflict_resolution") or {}).get("n_applicable"),
            "n_excluded": (result.get("conflict_resolution") or {}).get("n_excluded"),
        },
        "retrieval": result.get("retrieval"),
        "evidence_sentences": result.get("evidence_sentences") or [],
        "cascade": result.get("cascade"),
    }


def compact_retrieval_payload(result: dict) -> dict[str, Any]:
    hits = []
    for item in result.get("hits") or []:
        hits.append(
            {
                "chunk_id": item.get("chunk_id"),
                "doc_id": item.get("doc_id"),
                "section_path": item.get("section_path"),
                "char_start": item.get("char_start"),
                "char_end": item.get("char_end"),
                "text": item.get("text"),
                "title": item.get("title"),
                "source_type": item.get("source_type"),
                "scope_type": item.get("scope_type"),
                "scope_value": item.get("scope_value"),
                "effective_date": item.get("effective_date"),
                "expire_date": item.get("expire_date"),
                "score": item.get("score"),
                "retrieval_rank": item.get("retrieval_rank"),
                "dense_score": item.get("dense_score"),
                "bm25_score": item.get("bm25_score"),
            }
        )
    return {
        "ok": True,
        "retriever": result.get("retriever"),
        "embedding_model": result.get("embedding_model"),
        "embedding_dim": result.get("embedding_dim"),
        "chunking_name": result.get("chunking_name"),
        "bm25": result.get("bm25"),
        "rrf_k": result.get("rrf_k"),
        "rerank": result.get("rerank"),
        "query": result.get("query"),
        "top_k": result.get("top_k"),
        "hits": hits,
    }
