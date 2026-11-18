#!/usr/bin/env python3
"""按指定（或已锁定）切分写入 chunks，并调用锁定的 embedding 写入 chunk_embeddings。

失败时只报告错误类型，不打印密钥，不编造向量。优先使用本地向量缓存，避免重复计费。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.chunking import (  # noqa: E402
    chunk_sections,
    get_strategy,
    params_dict,
)
from app.config import get_settings  # noqa: E402
from app.corpus_io import load_corpus  # noqa: E402
from app.embed_cache import embed_texts_cached  # noqa: E402
from app.embeddings import LOCKED_DIM, LOCKED_MODEL, EmbeddingError  # noqa: E402
from app.index_store import replace_chunks  # noqa: E402
from app.pipeline import locked_chunking_params  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy",
        default="locked",
        help="切分策略名，或 locked（读取 reports/locked_retrieval.json，不存在则用 section_window_400）",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    settings = get_settings()
    if settings.embedding_model and settings.embedding_model != LOCKED_MODEL:
        print(json.dumps({"ok": False, "error_type": "embedding_lock_violation_model"}, ensure_ascii=False))
        return 2
    if settings.embedding_dim not in (None, LOCKED_DIM):
        print(json.dumps({"ok": False, "error_type": "embedding_lock_violation_dim"}, ensure_ascii=False))
        return 2

    if args.strategy == "locked":
        name, params = locked_chunking_params()
    else:
        name = args.strategy
        params = get_strategy(name)

    docs = load_corpus()
    chunks = chunk_sections(docs, params)
    if not chunks:
        print(json.dumps({"ok": False, "error_type": "no_chunks"}, ensure_ascii=False))
        return 3

    try:
        vectors = embed_texts_cached([c["text"] for c in chunks], model=LOCKED_MODEL, expected_dim=LOCKED_DIM)
    except EmbeddingError as exc:
        print(json.dumps({"ok": False, "error_type": exc.error_type, "stage": "embed"}, ensure_ascii=False))
        return 4

    with psycopg.connect(settings.dsn()) as conn:
        chunk_ids = replace_chunks(conn, chunks, vectors)

    report = {
        "ok": True,
        "n_docs": len(docs),
        "n_chunks": len(chunks),
        "n_embeddings": len(vectors),
        "n_ids": len(chunk_ids),
        "model": LOCKED_MODEL,
        "dim": LOCKED_DIM,
        "chunking_name": name,
        "chunking": params_dict(params, name=name),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
