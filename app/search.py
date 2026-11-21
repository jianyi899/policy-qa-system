"""密集检索（sqlite-vec 余弦距离）、BM25+RRF 融合，以及可选的 gte-rerank。

不使用 LangChain，不把其它排序称为 BM25。BM25 实现为 jieba + rank_bm25.BM25Okapi
（k1=1.5，b=0.75），与密集检索经 RRF（k=60）融合。
"""

from __future__ import annotations

from app.db import VECTOR_TABLE, pack_embedding
from app.rerank import RerankError, rerank_documents
from app.retrieval import (
    BM25_B,
    BM25_K1,
    RRF_K,
    BM25Index,
    build_bm25_index,
    ranks_from_scores,
    rrf_params_dict,
    rrf_scores,
)


def load_chunks(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, doc_id, section_path, char_start, char_end, text
        FROM chunks
        ORDER BY doc_id, section_path, char_start, id
        """
    ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "chunk_id": int(r["id"]),
            "doc_id": r["doc_id"],
            "section_path": r["section_path"],
            "char_start": int(r["char_start"]),
            "char_end": int(r["char_end"]),
            "text": r["text"],
        }
        for r in rows
    ]


def dense_search(
    conn,
    query_vector: list[float],
    *,
    k: int = 20,
) -> list[dict]:
    """按余弦距离返回前 k 条切片。得分 = 1 - distance。"""
    if k <= 0:
        return []
    rows = conn.execute(
        f"""
        SELECT e.chunk_id AS chunk_id, e.distance AS distance,
               c.doc_id AS doc_id, c.section_path AS section_path,
               c.char_start AS char_start, c.char_end AS char_end, c.text AS text
        FROM {VECTOR_TABLE} e
        JOIN chunks c ON c.id = e.chunk_id
        WHERE e.embedding MATCH ? AND k = ?
        """,
        (pack_embedding(query_vector), int(k)),
    ).fetchall()
    results = []
    for r in rows:
        distance = float(r["distance"])
        results.append(
            {
                "chunk_id": int(r["chunk_id"]),
                "doc_id": r["doc_id"],
                "section_path": r["section_path"],
                "char_start": int(r["char_start"]),
                "char_end": int(r["char_end"]),
                "text": r["text"],
                "distance": distance,
                "score": 1.0 - distance,
            }
        )
    results.sort(key=lambda x: (x["distance"], x["doc_id"], x["section_path"], x["char_start"], x["chunk_id"]))
    return results


def chunk_key(ch: dict) -> tuple:
    return (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"])


def dense_scores_aligned(conn, query_vector: list[float], corpus: list[dict]) -> list[float]:
    """返回与 corpus 对齐的密集相似度（1 - 余弦距离）。未召回的切片记为 0。"""
    if not corpus:
        return []
    hits = dense_search(conn, query_vector, k=len(corpus))
    by_id = {h["chunk_id"]: h["score"] for h in hits}
    return [float(by_id.get(c["chunk_id"], 0.0)) for c in corpus]


def rank_corpus(
    query: str,
    corpus: list[dict],
    dense_scores: list[float],
    *,
    retriever: str = "dense",
    rrf_k: int = RRF_K,
    bm25_index: BM25Index | None = None,
    rerank_model: str = "gte-rerank",
    rerank_candidates: int = 20,
    allow_rerank_fallback: bool = True,
) -> dict:
    """对已对齐的语料切片排序。hybrid_rerank 失败时抛出 RerankError。"""
    if retriever not in {"dense", "hybrid", "hybrid_rerank"}:
        raise ValueError(f"未知 retriever: {retriever}")
    n = len(corpus)
    if n == 0:
        return {
            "order": [],
            "scores": [],
            "score_note": "empty_corpus",
            "rerank_model": None,
            "rerank_fallback_from": None,
            "bm25_scores": None,
        }
    keys = [chunk_key(c) for c in corpus]
    if retriever == "dense":
        final_scores = list(dense_scores)
        score_note = "dense_cosine_sim"
        order = sorted(range(n), key=lambda i: (-final_scores[i], keys[i]))
        return {
            "order": order,
            "scores": final_scores,
            "score_note": score_note,
            "rerank_model": None,
            "rerank_fallback_from": None,
            "bm25_scores": None,
        }

    if bm25_index is None:
        bm25_index = build_bm25_index([c["text"] for c in corpus], k1=BM25_K1, b=BM25_B)
    dense_ranks = ranks_from_scores(dense_scores, higher_is_better=True, tie_keys=keys)
    bm25_scores = bm25_index.scores(query)
    bm25_ranks = ranks_from_scores(bm25_scores, higher_is_better=True, tie_keys=keys)
    fused = rrf_scores([dense_ranks, bm25_ranks], k=rrf_k)
    final_scores = list(fused)
    score_note = f"rrf_k={rrf_k}"
    order = sorted(range(n), key=lambda i: (-final_scores[i], keys[i]))
    used_model = None
    fallback_from = None

    if retriever == "hybrid_rerank":
        cand_n = min(rerank_candidates, len(order))
        cand_idx = order[:cand_n]
        rr = rerank_documents(
            query,
            [corpus[i]["text"] for i in cand_idx],
            model=rerank_model,
            top_n=cand_n,
            allow_fallback=allow_rerank_fallback,
        )
        used_model = rr["model"]
        fallback_from = rr["fallback_from"]
        reranked: list[tuple[int, float | None]] = []
        seen: set[int] = set()
        for item in rr["results"]:
            local = item["index"]
            if local in seen:
                continue
            seen.add(local)
            reranked.append((cand_idx[local], item["relevance_score"]))
        already = {i for i, _ in reranked}
        for i in cand_idx:
            if i not in already:
                reranked.append((i, None))
        tail = [i for i in order[cand_n:]]
        order = [i for i, _ in reranked] + tail
        display_scores = list(final_scores)
        for i, score in reranked:
            if score is not None:
                display_scores[i] = score
        final_scores = display_scores
        score_note = f"rrf_k={rrf_k}+rerank:{used_model}"

    return {
        "order": order,
        "scores": final_scores,
        "score_note": score_note,
        "rerank_model": used_model,
        "rerank_fallback_from": fallback_from,
        "bm25_scores": bm25_scores,
    }


def hits_from_ranking(
    corpus: list[dict],
    ranking: dict,
    *,
    k: int = 20,
) -> list[dict]:
    hits = []
    for rank, i in enumerate(ranking["order"][:k], start=1):
        item = dict(corpus[i])
        score = ranking["scores"][i]
        item["score"] = float(score) if score is not None else None
        item["retrieval_rank"] = rank
        item["score_note"] = ranking["score_note"]
        hits.append(item)
    return hits


def retrieval_description(
    retriever: str,
    *,
    rrf_k: int = RRF_K,
    rerank_model: str | None = None,
    rerank_candidates: int = 20,
) -> str:
    if retriever == "dense":
        return "纯密集检索，距离为 sqlite-vec 余弦距离（distance_metric=cosine），得分=1-距离。不加 BM25/RRF/rerank。"
    if retriever == "hybrid":
        return (
            f"密集检索与 BM25Okapi（jieba，k1={BM25_K1}，b={BM25_B}）"
            f"经 RRF（k={rrf_k}）融合。不加 rerank。"
        )
    if retriever == "hybrid_rerank":
        return (
            f"密集检索与 BM25Okapi（jieba，k1={BM25_K1}，b={BM25_B}）"
            f"经 RRF（k={rrf_k}）融合后，再对前 {rerank_candidates} 条调用 DashScope "
            f"{rerank_model or 'gte-rerank'}。"
        )
    raise ValueError(f"未知 retriever: {retriever}")
