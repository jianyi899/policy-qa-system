"""应用层检索：密集检索辅助、BM25Okapi（jieba，k1=1.5，b=0.75）与 RRF 融合。

不使用 LangChain。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi

jieba.setLogLevel(logging.WARNING)

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60


def tokenize(text: str) -> list[str]:
    return [tok.strip() for tok in jieba.cut(text) if tok.strip()]


@dataclass
class BM25Index:
    k1: float
    b: float
    tokenizer: str
    tokens: list[list[str]]
    _model: BM25Okapi

    def scores(self, query: str) -> list[float]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return [0.0] * len(self.tokens)
        raw = self._model.get_scores(q_tokens)
        return [float(x) for x in raw]


def build_bm25_index(
    texts: list[str],
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> BM25Index:
    # 先初始化词典，避免首次切词的日志干扰评测输出。
    jieba.initialize()
    tokens = [tokenize(t) for t in texts]
    # rank_bm25 在空语料上会出问题；调用方应保证至少有一条切片。
    model = BM25Okapi(tokens, k1=k1, b=b)
    return BM25Index(k1=k1, b=b, tokenizer="jieba", tokens=tokens, _model=model)


def ranks_from_scores(
    scores: list[float],
    *,
    higher_is_better: bool = True,
    tie_keys: list[tuple] | None = None,
) -> list[int]:
    """返回与 scores 对齐的 1-based 排名。同分时按 tie_keys 稳定排序。"""
    n = len(scores)
    keys = tie_keys if tie_keys is not None else [(i,) for i in range(n)]

    def sort_key(i: int):
        score_part = -scores[i] if higher_is_better else scores[i]
        return (score_part, keys[i])

    order = sorted(range(n), key=sort_key)
    ranks = [0] * n
    for rank, i in enumerate(order, start=1):
        ranks[i] = rank
    return ranks


def rrf_scores(
    rank_lists: list[list[int]],
    *,
    k: int = RRF_K,
) -> list[float]:
    """对多路 1-based 排名做 Reciprocal Rank Fusion。"""
    if not rank_lists:
        return []
    n = len(rank_lists[0])
    if any(len(r) != n for r in rank_lists):
        raise ValueError("RRF 各路排名长度必须一致")
    out = [0.0] * n
    for ranks in rank_lists:
        for i, r in enumerate(ranks):
            if r <= 0:
                raise ValueError("排名必须是从 1 开始的正整数")
            out[i] += 1.0 / (k + r)
    return out


def rrf_params_dict(*, k: int = RRF_K, k1: float = BM25_K1, b: float = BM25_B) -> dict:
    return {
        "fusion": "rrf",
        "rrf_k": k,
        "bm25": {
            "implementation": "rank_bm25.BM25Okapi",
            "tokenizer": "jieba",
            "k1": k1,
            "b": b,
        },
    }
