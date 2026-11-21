#!/usr/bin/env python3
"""可复现的检索评测：至少计算 Recall@5。

支持纯密集、BM25+RRF 融合，以及融合后再调用 gte-rerank。
拒答题不纳入 Recall@5 分母。本脚本不跑生成。
跑测试集前必须先核对封存 sha256，且须显式 --allow-test。
回归请用 scripts/check_regression.py；本脚本在开发集上默认不自动门禁，
可用 --check-regression 对照上次通过的结果。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import VECTOR_TABLE, connection  # noqa: E402
from app.embed_cache import embed_texts_cached  # noqa: E402
from app.embeddings import EmbeddingError, resolve_model_dim  # noqa: E402
from app.rerank import RerankError  # noqa: E402
from app.retrieval import BM25_B, BM25_K1, RRF_K, build_bm25_index, rrf_params_dict  # noqa: E402
from app.search import (  # noqa: E402
    dense_scores_aligned,
    hits_from_ranking,
    load_chunks,
    rank_corpus,
    retrieval_description,
    chunk_key,
)
from app.regression import check_regression  # noqa: E402
from eval.seal import verify_testset_seal  # noqa: E402

DEFAULT_BASELINE = ROOT / "reports" / "last_passed_dev_recall.json"


def load_jsonl(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


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
    load_dotenv(ROOT / ".env", override=False)
    settings = get_settings()
    model, dim = resolve_model_dim(settings)
    questions = [q for q in load_jsonl(ROOT / "eval" / "questions.jsonl") if q["split"] == split]
    map_file = mapping_path or (ROOT / "eval" / "annotation_mapping.json")
    mapping = json.loads(map_file.read_text(encoding="utf-8"))
    gold_by_id = {item["question_id"]: item["gold_chunks"] for item in mapping["items"]}

    answerable = [q for q in questions if not q.get("should_refuse") and q.get("type") != "refusal"]
    refusals = [q for q in questions if q.get("should_refuse") or q.get("type") == "refusal"]

    try:
        q_vectors = embed_texts_cached(
            [q["question"] for q in answerable],
            model=model,
            expected_dim=dim,
            settings=settings,
        )
    except EmbeddingError as exc:
        return {"ok": False, "error_type": exc.error_type, "stage": "query_embed", "split": split}

    used_rerank_model = None
    rerank_fallback_from = None

    try:
        with connection(read_only=True, settings=settings) as conn:
            n_vecs = conn.execute(f"SELECT COUNT(*) FROM {VECTOR_TABLE}").fetchone()[0]
            if int(n_vecs) == 0:
                return {"ok": False, "error_type": "no_chunk_embeddings", "split": split}
            corpus = load_chunks(conn)
            bm25_index = None
            if retriever in {"hybrid", "hybrid_rerank"}:
                bm25_index = build_bm25_index([c["text"] for c in corpus], k1=BM25_K1, b=BM25_B)

            per_question = []
            for q, vec in zip(answerable, q_vectors, strict=True):
                gold_chunks = gold_by_id.get(q["id"]) or []
                gold = {chunk_key(c) for c in gold_chunks}
                dense_scores = dense_scores_aligned(conn, vec, corpus)
                try:
                    ranking = rank_corpus(
                        q["question"],
                        corpus,
                        dense_scores,
                        retriever=retriever,
                        rrf_k=rrf_k,
                        bm25_index=bm25_index,
                        rerank_model=rerank_model,
                        rerank_candidates=rerank_candidates,
                        allow_rerank_fallback=allow_rerank_fallback,
                    )
                except RerankError as exc:
                    return {
                        "ok": False,
                        "error_type": exc.error_type,
                        "stage": "rerank",
                        "split": split,
                        "failed_question_id": q["id"],
                    }
                if ranking["rerank_model"]:
                    used_rerank_model = ranking["rerank_model"]
                if ranking["rerank_fallback_from"]:
                    rerank_fallback_from = ranking["rerank_fallback_from"]
                hits = hits_from_ranking(corpus, ranking, k=retrieve_k)
                retrieved = [chunk_key(h) for h in hits]
                rec = {
                    "id": q["id"],
                    "type": q["type"],
                    "cluster": q["cluster"],
                    "n_gold": len(gold),
                    "recall@5": recall_at_k(retrieved, gold, 5),
                    "hit@5": hit_at_k(retrieved, gold, 5),
                    "mrr@5": mrr_at_k(retrieved, gold, 5),
                    "ndcg@5": ndcg_at_k(retrieved, gold, 5),
                    "recall@10": recall_at_k(retrieved, gold, 10),
                    "hit@10": hit_at_k(retrieved, gold, 10),
                    "score_note": ranking["score_note"],
                    "retrieved": [
                        {
                            "rank": i,
                            "doc_id": h["doc_id"],
                            "section_path": h["section_path"],
                            "char_start": h["char_start"],
                            "char_end": h["char_end"],
                            "chunk_id": h["chunk_id"],
                            "score": h["score"],
                        }
                        for i, h in enumerate(hits[:k_report], start=1)
                    ],
                }
                per_question.append(rec)
    except EmbeddingError as exc:
        return {"ok": False, "error_type": exc.error_type, "stage": "retrieve", "split": split}

    def agg(metric: str) -> float | None:
        return mean([row[metric] for row in per_question])

    by_type: dict[str, list] = defaultdict(list)
    for row in per_question:
        by_type[row["type"]].append(row)

    def type_summary(items: list[dict]) -> dict:
        return {
            "n": len(items),
            "Recall@5": mean([x["recall@5"] for x in items]),
            "Hit@5": mean([x["hit@5"] for x in items]),
            "n_hit@5": int(sum(x["hit@5"] for x in items if not math.isnan(x["hit@5"]))),
        }

    failures = [r for r in per_question if r["hit@5"] < 1]
    n_hit = int(sum(r["hit@5"] for r in per_question if not math.isnan(r["hit@5"])))
    report = {
        "ok": True,
        "split": split,
        "retriever": retriever,
        "definitions": {
            "Recall@5": "前 5 条中命中的金标切片数 / 该题金标切片数，再对可答题宏平均。拒答题不进分母。",
            "Hit@5": "前 5 条至少命中 1 个金标切片记为 1，否则为 0，再宏平均。",
            "MRR@5": "第一个金标切片排名的倒数，未命中为 0，再宏平均。",
            "nDCG@5": "相关切片相关性按二元，再宏平均。",
            "retrieval": retrieval_description(
                retriever,
                rrf_k=rrf_k,
                rerank_model=used_rerank_model or rerank_model,
                rerank_candidates=rerank_candidates,
            ),
        },
        "n_questions_in_split": len(questions),
        "n_answerable": len(answerable),
        "n_refusal_excluded": len(refusals),
        "n_used_for_recall": len(per_question),
        "primary_metric": "Recall@5",
        "metrics": {
            "Recall@5": agg("recall@5"),
            "Hit@5": agg("hit@5"),
            "MRR@5": agg("mrr@5"),
            "nDCG@5": agg("ndcg@5"),
            "Recall@10": agg("recall@10"),
            "Hit@10": agg("hit@10"),
            "n_hit@5": n_hit,
            "n_answerable_for_hit": len(per_question),
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
            "correct": "not_run",
            "report": f"未测 / {len(refusals)}" if refusals else "0 / 0",
            "generation": "not_run",
            "note": "本段只评检索，不跑生成；拒答题按「对了几道 / 共几道」汇报，不设 90% 目标，也不计入 Recall@5。",
        },
        "settings": {
            "retriever": retriever,
            "embedding_model": model,
            "embedding_dim": dim,
            "n_chunks": len(corpus),
            "distance": "sqlite-vec cosine",
            "score": "1 - cosine_distance" if retriever == "dense" else None,
            "bm25": (
                False
                if retriever == "dense"
                else {
                    "implementation": "rank_bm25.BM25Okapi",
                    "tokenizer": "jieba",
                    "k1": BM25_K1,
                    "b": BM25_B,
                }
            ),
            "rrf": False if retriever == "dense" else rrf_params_dict(k=rrf_k),
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
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="上次通过的开发集评测（含 per_question），用于回归。",
    )
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="在开发集上对照上次通过的 Hit@5 / Recall@5，净减少超过 1 题则失败。",
    )
    parser.add_argument(
        "--skip-regression",
        action="store_true",
        help="兼容旧参数：默认已跳过回归，此开关无额外效果。",
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
    if args.split == "dev" and args.check_regression and report.get("ok"):
        if not args.baseline.is_file():
            regression = {
                "ok": False,
                "passed": False,
                "checked": False,
                "error_type": "missing_baseline",
                "path": str(args.baseline),
            }
        else:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            regression = check_regression(baseline, report, baseline_path=args.baseline)
        report["regression"] = regression
        if not regression.get("passed"):
            report["ok"] = False
            report["error_type"] = regression.get("error_type") or "regression_failed"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
