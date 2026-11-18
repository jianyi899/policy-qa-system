#!/usr/bin/env python3
"""可复现的检索评测：至少计算 Recall@5。

支持纯密集、BM25+RRF 融合、以及融合后再调用 gte-rerank。
拒答题不纳入 Recall@5 分母。本脚本不跑生成。
跑测试集前必须先核对封存 sha256。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.embed_cache import embed_texts_cached  # noqa: E402
from app.embeddings import (  # noqa: E402
    LOCKED_DIM,
    LOCKED_MODEL,
    EmbeddingError,
)
from app.index_store import vector_literal  # noqa: E402
from app.rerank import RerankError, rerank_documents  # noqa: E402
from app.retrieval import (  # noqa: E402
    BM25_B,
    BM25_K1,
    RRF_K,
    build_bm25_index,
    ranks_from_scores,
    rrf_params_dict,
    rrf_scores,
)
from eval.seal import verify_testset_seal  # noqa: E402


def load_jsonl(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def chunk_key(ch: dict) -> tuple:
    return (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"])


def recall_at_k(retrieved: list[tuple], gold: set[tuple], k: int) -> float:
    if not gold:
        return float("nan")
    top = set(retrieved[:k])
    return len(top & gold) / len(gold)


def hit_at_k(retrieved: list[tuple], gold: set[tuple], k: int) -> float:
    if not gold:
        return float("nan")
    return 1.0 if set(retrieved[:k]) & gold else 0.0


def mrr_at_k(retrieved: list[tuple], gold: set[tuple], k: int) -> float:
    if not gold:
        return float("nan")
    for i, key in enumerate(retrieved[:k], start=1):
        if key in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: list[tuple], gold: set[tuple], k: int) -> float:
    if not gold:
        return float("nan")
    dcg = 0.0
    for i, key in enumerate(retrieved[:k], start=1):
        if key in gold:
            dcg += 1.0 / math.log2(i + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def mean(xs: list[float]) -> float | None:
    vals = [x for x in xs if not math.isnan(x)]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _retrieval_description(retriever: str, rrf_k: int, rerank_model: str | None, rerank_k: int) -> str:
    if retriever == "dense":
        return "纯密集检索，距离为 pgvector 余弦距离 <=>，得分=1-距离。不加 BM25/RRF/rerank。"
    if retriever == "hybrid":
        return (
            f"密集检索与 BM25Okapi（jieba，k1={BM25_K1}，b={BM25_B}）"
            f"经 RRF（k={rrf_k}）融合。不加 rerank。"
        )
    if retriever == "hybrid_rerank":
        return (
            f"密集检索与 BM25Okapi（jieba，k1={BM25_K1}，b={BM25_B}）"
            f"经 RRF（k={rrf_k}）融合后，再对前 {rerank_k} 条调用 DashScope {rerank_model or 'gte-rerank'}。"
        )
    raise ValueError(f"未知 retriever: {retriever}")


def evaluate(
    split: str,
    *,
    retriever: str = "dense",
    mapping_path: Path | None = None,
    k_report: int = 5,
    retrieve_k: int = 20,
    rrf_k: int = RRF_K,
    rerank_model: str = "gte-rerank",
    rerank_candidates: int = 20,
    allow_rerank_fallback: bool = True,
) -> dict:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    questions = [q for q in load_jsonl(ROOT / "eval" / "questions.jsonl") if q["split"] == split]
    map_file = mapping_path or (ROOT / "eval" / "annotation_mapping.json")
    mapping = json.loads(map_file.read_text(encoding="utf-8"))
    gold_by_id = {item["question_id"]: item["gold_chunks"] for item in mapping["items"]}

    answerable = [q for q in questions if not q.get("should_refuse") and q.get("type") != "refusal"]
    refusals = [q for q in questions if q.get("should_refuse") or q.get("type") == "refusal"]

    try:
        q_vectors = embed_texts_cached(
            [q["question"] for q in answerable],
            model=LOCKED_MODEL,
            expected_dim=LOCKED_DIM,
        )
    except EmbeddingError as exc:
        return {"ok": False, "error_type": exc.error_type, "stage": "query_embed", "split": split}

    used_rerank_model = None
    rerank_fallback_from = None

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*), MIN(dim), MAX(dim), MIN(model_name), MAX(model_name) FROM chunk_embeddings"
            )
            n_emb, min_dim, max_dim, min_model, max_model = cur.fetchone()
            if n_emb == 0:
                return {"ok": False, "error_type": "no_chunk_embeddings", "split": split}
            if min_dim != LOCKED_DIM or max_dim != LOCKED_DIM or min_model != LOCKED_MODEL or max_model != LOCKED_MODEL:
                return {
                    "ok": False,
                    "error_type": "embedding_store_mismatch",
                    "split": split,
                    "store": {"n": n_emb, "min_dim": min_dim, "max_dim": max_dim, "model": min_model},
                }
            cur.execute(
                """
                SELECT c.id, c.doc_id, c.section_path, c.char_start, c.char_end, c.text
                FROM chunks c
                JOIN chunk_embeddings e ON e.chunk_id = c.id
                ORDER BY c.doc_id, c.section_path, c.char_start, c.id
                """
            )
            corpus_rows = cur.fetchall()
            corpus = [
                {
                    "chunk_id": r[0],
                    "doc_id": r[1],
                    "section_path": r[2],
                    "char_start": r[3],
                    "char_end": r[4],
                    "text": r[5],
                }
                for r in corpus_rows
            ]
            keys = [chunk_key(c) for c in corpus]
            tie_keys = keys[:]

            bm25_index = None
            if retriever in {"hybrid", "hybrid_rerank"}:
                bm25_index = build_bm25_index([c["text"] for c in corpus], k1=BM25_K1, b=BM25_B)

            per_question = []
            for q, vec in zip(answerable, q_vectors, strict=True):
                lit = vector_literal(vec)
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
                dense_ranks = ranks_from_scores(dense_scores, higher_is_better=True, tie_keys=tie_keys)

                if retriever == "dense":
                    final_scores = dense_scores
                    score_note = "dense_cosine_sim"
                    order = sorted(
                        range(len(corpus)),
                        key=lambda i: (-final_scores[i], keys[i]),
                    )
                else:
                    assert bm25_index is not None
                    bm25_scores = bm25_index.scores(q["question"])
                    bm25_ranks = ranks_from_scores(bm25_scores, higher_is_better=True, tie_keys=tie_keys)
                    fused = rrf_scores([dense_ranks, bm25_ranks], k=rrf_k)
                    final_scores = fused
                    score_note = f"rrf_k={rrf_k}"
                    order = sorted(
                        range(len(corpus)),
                        key=lambda i: (-final_scores[i], keys[i]),
                    )

                if retriever == "hybrid_rerank":
                    cand_n = min(rerank_candidates, len(order))
                    cand_idx = order[:cand_n]
                    try:
                        rr = rerank_documents(
                            q["question"],
                            [corpus[i]["text"] for i in cand_idx],
                            model=rerank_model,
                            top_n=cand_n,
                            allow_fallback=allow_rerank_fallback,
                        )
                    except RerankError as exc:
                        return {
                            "ok": False,
                            "error_type": exc.error_type,
                            "stage": "rerank",
                            "split": split,
                            "failed_question_id": q["id"],
                        }
                    used_rerank_model = rr["model"]
                    if rr["fallback_from"]:
                        rerank_fallback_from = rr["fallback_from"]
                    reranked = []
                    seen = set()
                    for item in rr["results"]:
                        local = item["index"]
                        if local in seen:
                            continue
                        seen.add(local)
                        reranked.append((cand_idx[local], item["relevance_score"]))
                    # 未返回的候选保持原融合顺序，接在后面
                    for i in cand_idx:
                        if i not in {x[0] for x in reranked}:
                            reranked.append((i, None))
                    tail = [i for i in order[cand_n:]]
                    order = [i for i, _ in reranked] + tail
                    # 用 rerank 分覆盖前 cand_n 的展示分
                    display_scores = list(final_scores)
                    for i, score in reranked:
                        if score is not None:
                            display_scores[i] = score
                    final_scores = display_scores
                    score_note = f"rrf_k={rrf_k}+rerank:{used_rerank_model}"

                ranked = order[:retrieve_k]
                retrieved = [keys[i] for i in ranked]
                retrieved_view = [
                    {
                        "chunk_id": corpus[i]["chunk_id"],
                        "doc_id": corpus[i]["doc_id"],
                        "section_path": corpus[i]["section_path"],
                        "char_start": corpus[i]["char_start"],
                        "char_end": corpus[i]["char_end"],
                        "score": float(final_scores[i]) if final_scores[i] is not None else None,
                    }
                    for i in ranked
                ]
                gold_chunks = gold_by_id.get(q["id"], [])
                gold = {chunk_key(c) for c in gold_chunks}
                rec = {
                    "id": q["id"],
                    "type": q["type"],
                    "cluster": q["cluster"],
                    "n_gold_chunks": len(gold),
                    "recall@5": recall_at_k(retrieved, gold, 5),
                    "hit@5": hit_at_k(retrieved, gold, 5),
                    "mrr@5": mrr_at_k(retrieved, gold, 5),
                    "ndcg@5": ndcg_at_k(retrieved, gold, 5),
                    "recall@10": recall_at_k(retrieved, gold, 10),
                    "hit@10": hit_at_k(retrieved, gold, 10),
                    "retrieved_top5": retrieved_view[:5],
                    "missing_gold_in_top5": [
                        {
                            "doc_id": c["doc_id"],
                            "section_path": c["section_path"],
                            "char_start": c["char_start"],
                            "char_end": c["char_end"],
                        }
                        for c in gold_chunks
                        if chunk_key(c) not in set(retrieved[:5])
                    ],
                    "score_note": score_note,
                }
                per_question.append(rec)

    def agg(metric: str) -> dict:
        vals = [r[metric] for r in per_question]
        valid = [v for v in vals if not math.isnan(v)]
        return {
            "mean": mean(vals),
            "n": len(valid),
            "n_undefined_no_gold": len(vals) - len(valid),
        }

    failures = [r for r in per_question if r["n_gold_chunks"] > 0 and r["hit@5"] < 1]
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in per_question:
        by_type[r["type"]].append(r)

    def type_summary(items: list[dict]) -> dict:
        return {
            "n": len(items),
            "recall@5_mean": mean([x["recall@5"] for x in items]),
            "hit@5_mean": mean([x["hit@5"] for x in items]),
        }

    primary = agg("recall@5")
    report = {
        "ok": True,
        "split": split,
        "retriever": retriever,
        "metric_definitions": {
            "Recall@5": "对每道有金标切片的可答题，计算 |gold ∩ top5| / |gold|，再对题目做宏平均。分母=有金标切片的可答题数。拒答题不计入。",
            "Hit@5": "对每道有金标切片的可答题，top5 中至少有一条相关切片记 1，否则记 0，再宏平均。分母同上。",
            "MRR@5": "第一条相关切片排名的倒数，未命中为 0，再宏平均。",
            "nDCG@5": "相关切片相关性按二元，再宏平均。",
            "retrieval": _retrieval_description(retriever, rrf_k, used_rerank_model or rerank_model, rerank_candidates),
        },
        "n_questions_in_split": len(questions),
        "n_answerable": len(answerable),
        "n_refusal_excluded": len(refusals),
        "n_used_for_recall": primary["n"],
        "primary_metric": "Recall@5",
        "metrics": {
            "Recall@5": agg("recall@5"),
            "Hit@5": agg("hit@5"),
            "MRR@5": agg("mrr@5"),
            "nDCG@5": agg("ndcg@5"),
            "Recall@10": agg("recall@10"),
            "Hit@10": agg("hit@10"),
        },
        "by_type": {t: type_summary(items) for t, items in sorted(by_type.items())},
        "k_eval": k_report,
        "k_retrieved": retrieve_k,
        "n_failures_hit@5": len(failures),
        "failure_ids": [r["id"] for r in failures],
        "per_question": per_question,
        "refusal": {
            "n": len(refusals),
            "ids": [q["id"] for q in refusals],
            "generation": "not_run",
            "note": "本段只评检索，不跑生成；拒答题不设正确率目标，也不计入 Recall@5。",
        },
        "settings": {
            "retriever": retriever,
            "embedding_model": LOCKED_MODEL,
            "embedding_dim": LOCKED_DIM,
            "n_chunks": len(corpus),
            "rrf": rrf_params_dict(k=rrf_k) if retriever in {"hybrid", "hybrid_rerank"} else False,
            "rerank": (
                {
                    "requested_model": rerank_model,
                    "used_model": used_rerank_model,
                    "fallback_from": rerank_fallback_from,
                    "candidates": rerank_candidates,
                }
                if retriever == "hybrid_rerank"
                else False
            ),
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--retriever", choices=["dense", "hybrid", "hybrid_rerank"], default="dense")
    parser.add_argument("--mapping", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--rrf-k", type=int, default=RRF_K)
    parser.add_argument("--rerank-model", default="gte-rerank")
    parser.add_argument("--rerank-candidates", type=int, default=20)
    parser.add_argument(
        "--allow-test",
        action="store_true",
        help="允许在核对测试集封存哈希后评测测试集。不得用于调参。",
    )
    args = parser.parse_args()
    if args.split == "test":
        if not args.allow_test:
            print(json.dumps({"ok": False, "error_type": "test_split_requires_allow_test"}, ensure_ascii=False))
            return 5
        seal = verify_testset_seal()
        if not seal["ok"]:
            print(json.dumps({"ok": False, "error_type": "testset_seal_mismatch", "seal": seal}, ensure_ascii=False, indent=2))
            return 6
    report = evaluate(
        args.split,
        retriever=args.retriever,
        mapping_path=args.mapping,
        rrf_k=args.rrf_k,
        rerank_model=args.rerank_model,
        rerank_candidates=args.rerank_candidates,
    )
    if args.split == "test":
        report["testset_seal"] = verify_testset_seal()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
