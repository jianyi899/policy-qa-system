"""按已锁定检索链路取回政策切片：section_window_400 + 密集与 BM25 的 RRF 融合，不加 rerank。

不更换 embedding，不改 BM25 / RRF 参数。
"""

from __future__ import annotations

import psycopg

from app.config import get_settings
from app.embed_cache import embed_texts_cached
from app.embeddings import LOCKED_DIM, LOCKED_MODEL, EmbeddingError
from app.index_store import vector_literal
from app.pipeline import load_locked_pipeline
from app.retrieval import (
    BM25_B,
    BM25_K1,
    RRF_K,
    build_bm25_index,
    ranks_from_scores,
    rrf_scores,
)

_LOCK = None


def locked_params() -> dict:
    global _LOCK
    if _LOCK is None:
        _LOCK = load_locked_pipeline()
    return _LOCK


def _assert_lock(pipe: dict) -> None:
    if pipe.get("embedding_model") != LOCKED_MODEL or int(pipe.get("embedding_dim") or 0) != LOCKED_DIM:
        raise EmbeddingError("embedding_lock_violation")
    if pipe.get("retriever") not in {"hybrid", None} and pipe.get("retrieval") != "hybrid":
        # 锁定文件写的是 hybrid；若被改掉则拒绝继续，以免偏离第三段结论。
        if not (pipe.get("bm25") and pipe.get("rrf") and not pipe.get("rerank")):
            raise RuntimeError("retrieval_lock_violation")
    if pipe.get("rerank"):
        raise RuntimeError("retrieval_lock_violation")


def retrieve(query: str, *, top_k: int = 20, use_cache: bool = True) -> dict:
    """对单条问题跑锁定的 hybrid 检索，并附带文档的范围 / 来源 / 生效日。"""
    from app.cache import compact_retrieval_payload, get_json, retrieval_key, set_json
    from app.observability import observation

    pipe = locked_params()
    _assert_lock(pipe)
    settings = get_settings()
    cache_key = retrieval_key(query, top_k=top_k) if use_cache else None
    if cache_key:
        cached = get_json(cache_key)
        if cached and cached.get("hits"):
            cached = dict(cached)
            cached["cache_hit"] = True
            cached["cache_key"] = cache_key
            with observation(
                name="hybrid_retrieve",
                as_type="retriever",
                input={"query": query, "top_k": top_k, "cache_hit": True},
                output={"n_hits": len(cached.get("hits") or []), "cache_hit": True},
                metadata={"retriever": "hybrid", "cache_hit": True},
            ):
                return cached
    vectors = embed_texts_cached([query], model=LOCKED_MODEL, expected_dim=LOCKED_DIM)
    qvec = vectors[0]
    lit = vector_literal(qvec)

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.doc_id, c.section_path, c.char_start, c.char_end, c.text,
                       d.title, d.source_type, d.scope_type, d.scope_value,
                       d.effective_date, d.expire_date
                FROM chunks c
                JOIN chunk_embeddings e ON e.chunk_id = c.id
                JOIN documents d ON d.doc_id = c.doc_id
                ORDER BY c.doc_id, c.section_path, c.char_start, c.id
                """
            )
            rows = cur.fetchall()
            if not rows:
                raise RuntimeError("no_chunk_embeddings")
            corpus = [
                {
                    "chunk_id": r[0],
                    "doc_id": r[1],
                    "section_path": r[2],
                    "char_start": r[3],
                    "char_end": r[4],
                    "text": r[5],
                    "title": r[6],
                    "source_type": r[7],
                    "scope_type": r[8],
                    "scope_value": r[9],
                    "effective_date": r[10].isoformat() if r[10] else None,
                    "expire_date": r[11].isoformat() if r[11] else None,
                }
                for r in rows
            ]
            keys = [
                (c["doc_id"], c["section_path"], c["char_start"], c["char_end"])
                for c in corpus
            ]
            bm25_index = build_bm25_index([c["text"] for c in corpus], k1=BM25_K1, b=BM25_B)
            cur.execute(
                """
                SELECT c.id, (1 - (e.embedding <=> %s::vector)) AS score
                FROM chunk_embeddings e
                JOIN chunks c ON c.id = e.chunk_id
                ORDER BY e.embedding <=> %s::vector, c.id
                """,
                (lit, lit),
            )
            dense_pairs = cur.fetchall()

    dense_score_by_id = {int(r[0]): float(r[1]) if r[1] is not None else 0.0 for r in dense_pairs}
    dense_scores = [dense_score_by_id.get(c["chunk_id"], 0.0) for c in corpus]
    dense_ranks = ranks_from_scores(dense_scores, higher_is_better=True, tie_keys=keys)
    bm25_scores = bm25_index.scores(query)
    bm25_ranks = ranks_from_scores(bm25_scores, higher_is_better=True, tie_keys=keys)
    fused = rrf_scores([dense_ranks, bm25_ranks], k=RRF_K)
    order = sorted(range(len(corpus)), key=lambda i: (-fused[i], keys[i]))
    hits = []
    for rank, i in enumerate(order[:top_k], start=1):
        item = dict(corpus[i])
        item["score"] = float(fused[i])
        item["retrieval_rank"] = rank
        item["dense_score"] = float(dense_scores[i])
        item["bm25_score"] = float(bm25_scores[i])
        hits.append(item)
    result = {
        "ok": True,
        "retriever": "hybrid",
        "embedding_model": LOCKED_MODEL,
        "embedding_dim": LOCKED_DIM,
        "chunking_name": pipe.get("chunking_name"),
        "bm25": {"tokenizer": "jieba", "k1": BM25_K1, "b": BM25_B},
        "rrf_k": RRF_K,
        "rerank": False,
        "query": query,
        "top_k": top_k,
        "hits": hits,
        "cache_hit": False,
    }
    if cache_key:
        set_json(cache_key, compact_retrieval_payload(result))
        result["cache_key"] = cache_key
    with observation(
        name="hybrid_retrieve",
        as_type="retriever",
        input={"query": query, "top_k": top_k, "cache_hit": False},
        output={
            "n_hits": len(hits),
            "cache_hit": False,
            "top_doc_ids": [h.get("doc_id") for h in hits[:5]],
            "top1_rrf": hits[0]["score"] if hits else None,
        },
        metadata={"retriever": "hybrid", "cache_hit": False},
    ):
        pass
    return result
