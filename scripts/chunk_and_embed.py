#!/usr/bin/env python3
"""按默认切分写入 chunks，并调用锁定的 embedding 写入 chunk_embeddings。

失败时只报告错误类型，不打印密钥，不编造向量。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.chunking import ChunkingParams, chunk_sections, params_dict  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.corpus_io import load_corpus  # noqa: E402
from app.embeddings import (  # noqa: E402
    LOCKED_DIM,
    LOCKED_MODEL,
    EmbeddingError,
    embed_texts,
)


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(format(x, ".8g") for x in vec) + "]"


def main() -> int:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    if settings.embedding_model and settings.embedding_model != LOCKED_MODEL:
        print(json.dumps({"ok": False, "error_type": "embedding_lock_violation_model"}, ensure_ascii=False))
        return 2
    if settings.embedding_dim not in (None, LOCKED_DIM):
        print(json.dumps({"ok": False, "error_type": "embedding_lock_violation_dim"}, ensure_ascii=False))
        return 2

    docs = load_corpus()
    params = ChunkingParams()
    chunks = chunk_sections(docs, params)
    if not chunks:
        print(json.dumps({"ok": False, "error_type": "no_chunks"}, ensure_ascii=False))
        return 3

    try:
        vectors = embed_texts([c["text"] for c in chunks], model=LOCKED_MODEL, expected_dim=LOCKED_DIM)
    except EmbeddingError as exc:
        print(json.dumps({"ok": False, "error_type": exc.error_type, "stage": "embed"}, ensure_ascii=False))
        return 4

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunk_embeddings")
            cur.execute("DELETE FROM chunks")
            chunk_ids = []
            for ch in chunks:
                cur.execute(
                    """
                    INSERT INTO chunks (doc_id, section_path, char_start, char_end, text)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"], ch["text"]),
                )
                chunk_ids.append(cur.fetchone()[0])
            for chunk_id, vec in zip(chunk_ids, vectors, strict=True):
                cur.execute(
                    """
                    INSERT INTO chunk_embeddings (chunk_id, model_name, dim, embedding)
                    VALUES (%s, %s, %s, %s::vector)
                    """,
                    (chunk_id, LOCKED_MODEL, LOCKED_DIM, vector_literal(vec)),
                )
        conn.commit()

    report = {
        "ok": True,
        "n_docs": len(docs),
        "n_chunks": len(chunks),
        "n_embeddings": len(vectors),
        "model": LOCKED_MODEL,
        "dim": LOCKED_DIM,
        "chunking": params_dict(params),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
