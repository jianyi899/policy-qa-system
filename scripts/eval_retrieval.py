#!/usr/bin/env python3
"""可复现的检索评测：至少计算 Recall@5。

本阶段 B0 只做纯密集检索，不加 BM25 / RRF / rerank。
拒答题不纳入 Recall@5 分母；B0 不跑生成。
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
from app.embeddings import (  # noqa: E402
    LOCKED_DIM,
    LOCKED_MODEL,
    EmbeddingError,
    embed_texts,
)


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(format(x, ".8g") for x in vec) + "]"


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


def evaluate(split: str, k_report: int = 5) -> dict:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    questions = [q for q in load_jsonl(ROOT / "eval" / "questions.jsonl") if q["split"] == split]
    mapping = json.loads((ROOT / "eval" / "annotation_mapping.json").read_text(encoding="utf-8"))
    gold_by_id = {item["question_id"]: item["gold_chunks"] for item in mapping["items"]}

    answerable = [q for q in questions if not q.get("should_refuse") and q.get("type") != "refusal"]
    refusals = [q for q in questions if q.get("should_refuse") or q.get("type") == "refusal"]

    try:
        q_vectors = embed_texts([q["question"] for q in answerable], model=LOCKED_MODEL, expected_dim=LOCKED_DIM)
    except EmbeddingError as exc:
        return {"ok": False, "error_type": exc.error_type, "stage": "query_embed", "split": split}

    retrieve_k = 20
    per_question = []
    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            # 确认索引中的模型与维度
            cur.execute("SELECT COUNT(*), MIN(dim), MAX(dim), MIN(model_name), MAX(model_name) FROM chunk_embeddings")
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
            for q, vec in zip(answerable, q_vectors, strict=True):
                lit = vector_literal(vec)
                cur.execute(
                    """
                    SELECT c.id, c.doc_id, c.section_path, c.char_start, c.char_end,
                           (1 - (e.embedding <=> %s::vector)) AS score
                    FROM chunk_embeddings e
                    JOIN chunks c ON c.id = e.chunk_id
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (lit, lit, retrieve_k),
                )
                rows = cur.fetchall()
                retrieved = [(r[1], r[2], r[3], r[4]) for r in rows]
                retrieved_view = [
                    {
                        "chunk_id": r[0],
                        "doc_id": r[1],
                        "section_path": r[2],
                        "char_start": r[3],
                        "char_end": r[4],
                        "score": float(r[5]) if r[5] is not None else None,
                    }
                    for r in rows
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

    # 主指标 Recall@5：可答题集合上的宏平均集合召回（|R∩top5|/|R|）。
    # 同时报告 hit@5（至少命中一条相关切片的题目比例），分母均为有金标切片的可答题。
    primary = agg("recall@5")
    report = {
        "ok": True,
        "split": split,
        "metric_definitions": {
            "Recall@5": "对每道有金标切片的可答题，计算 |gold ∩ top5| / |gold|，再对题目做宏平均。分母=有金标切片的可答题数。拒答题不计入。",
            "Hit@5": "对每道有金标切片的可答题，top5 中至少有一条相关切片记 1，否则记 0，再宏平均。分母同上。",
            "MRR@5": "第一条相关切片排名的倒数，未命中为 0，再宏平均。",
            "nDCG@5": "相关切片相关性按二元，再宏平均。",
            "retrieval": "纯密集检索，距离为 pgvector 余弦距离 <=>，得分=1-距离。不加 BM25/RRF/rerank。",
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
            "note": "B0 只评检索，不跑生成；拒答题不设正确率目标，也不计入 Recall@5。",
        },
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    if args.split == "test":
        print(json.dumps({"ok": False, "error_type": "test_split_forbidden_in_this_phase"}, ensure_ascii=False))
        return 5
    report = evaluate(args.split)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
