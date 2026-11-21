"""建库后写入语料与 embedding。指纹未变则跳过，避免重复写入和重复调用 embedding。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.chunking import chunk_sections, get_strategy, params_dict
from app.config import Settings, get_settings
from app.corpus_io import EXPECTED_DOCS, load_corpus
from app.db import VECTOR_TABLE, connection, pack_embedding
from app.embed_cache import embed_texts_cached
from app.embeddings import EmbeddingError, api_usage, reset_api_counters, resolve_model_dim
from app.pipeline import load_locked_pipeline

DEFAULT_CHUNKING = "section_window_400"


def resolve_chunking_name(chunking_name: str | None = None) -> str:
    if chunking_name:
        return chunking_name
    locked = load_locked_pipeline().get("chunking_name")
    return locked or DEFAULT_CHUNKING


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def corpus_fingerprint(
    docs: list[dict],
    *,
    model: str,
    dim: int,
    chunking_name: str,
    chunks: list[dict],
) -> str:
    payload = {
        "docs": [
            {
                "doc_id": d["doc_id"],
                "title": d["title"],
                "source_type": d["source_type"],
                "scope_type": d["scope_type"],
                "scope_value": d["scope_value"],
                "effective_date": d["effective_date"],
                "expire_date": d["expire_date"],
                "body": d["body"],
            }
            for d in sorted(docs, key=lambda x: x["doc_id"])
        ],
        "chunk_keys": [
            [c["doc_id"], c["section_path"], c["char_start"], c["char_end"], c["text"]]
            for c in chunks
        ],
        "embedding_model": model,
        "embedding_dim": dim,
        "chunking_name": chunking_name,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_state(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT fingerprint, embedding_model, embedding_dim, chunking_name,
               n_docs, n_chunks, n_embeddings, ingested_at
        FROM ingest_state WHERE id = 1
        """
    ).fetchone()
    if not row:
        return None
    return {
        "fingerprint": row["fingerprint"],
        "embedding_model": row["embedding_model"],
        "embedding_dim": int(row["embedding_dim"]),
        "chunking_name": row["chunking_name"],
        "n_docs": int(row["n_docs"]),
        "n_chunks": int(row["n_chunks"]),
        "n_embeddings": int(row["n_embeddings"]),
        "ingested_at": row["ingested_at"],
    }


def _counts_match(conn, n_docs: int, n_chunks: int) -> bool:
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    vecs = conn.execute(f"SELECT COUNT(*) FROM {VECTOR_TABLE}").fetchone()[0]
    return int(docs) == n_docs and int(chunks) == n_chunks and int(vecs) == n_chunks


def _replace_corpus(conn, docs: list[dict], chunks: list[dict], vectors: list[list[float]], model: str, dim: int) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("chunks 与 vectors 数量不一致")
    conn.execute(f"DELETE FROM {VECTOR_TABLE}")
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM sections")
    conn.execute("DELETE FROM documents")
    for doc in docs:
        conn.execute(
            """
            INSERT INTO documents (
                doc_id, title, source_type, scope_type, scope_value,
                effective_date, expire_date, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["doc_id"],
                doc["title"],
                doc["source_type"],
                doc["scope_type"],
                doc["scope_value"],
                doc["effective_date"],
                doc["expire_date"],
                doc["body"],
            ),
        )
        for sec in doc["sections"]:
            conn.execute(
                """
                INSERT INTO sections (
                    doc_id, section_path, heading, char_start, char_end, body
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    doc["doc_id"],
                    sec["section_path"],
                    sec["heading"],
                    sec["char_start"],
                    sec["char_end"],
                    sec["body"],
                ),
            )
    chunk_ids: list[int] = []
    for ch in chunks:
        cur = conn.execute(
            """
            INSERT INTO chunks (doc_id, section_path, char_start, char_end, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"], ch["text"]),
        )
        chunk_ids.append(int(cur.lastrowid))
    for chunk_id, vec in zip(chunk_ids, vectors, strict=True):
        if len(vec) != dim:
            raise EmbeddingError(f"unexpected_dim_{len(vec)}")
        conn.execute(
            f"INSERT INTO {VECTOR_TABLE}(chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, pack_embedding(vec)),
        )


def ingest_corpus(settings: Settings | None = None, *, chunking_name: str | None = None) -> dict:
    """写入语料。若指纹与库内记录一致则跳过 embedding。"""
    if settings is None:
        settings = get_settings()
    chunking_name = resolve_chunking_name(chunking_name)
    reset_api_counters()
    model, dim = resolve_model_dim(settings)
    docs = load_corpus()
    params = get_strategy(chunking_name)
    chunks = chunk_sections(docs, params)
    fingerprint = corpus_fingerprint(
        docs, model=model, dim=dim, chunking_name=chunking_name, chunks=chunks
    )

    with connection(settings=settings) as conn:
        state = _read_state(conn)
        if (
            state
            and state["fingerprint"] == fingerprint
            and state["embedding_model"] == model
            and state["embedding_dim"] == dim
            and _counts_match(conn, len(docs), len(chunks))
        ):
            usage = api_usage()
            return {
                "skipped": True,
                "reason": "fingerprint_unchanged",
                "n_docs": len(docs),
                "n_chunks": len(chunks),
                "n_embeddings": state["n_embeddings"],
                "embedding_model": model,
                "embedding_dim": dim,
                "chunking_name": chunking_name,
                "chunking": params_dict(params, name=chunking_name),
                "fingerprint": fingerprint,
                "expected_docs": list(EXPECTED_DOCS),
                "ingested_at": state["ingested_at"],
                **usage,
            }

        vectors = embed_texts_cached([c["text"] for c in chunks], model=model, expected_dim=dim, settings=settings)
        _replace_corpus(conn, docs, chunks, vectors, model, dim)
        ingested_at = _now_iso()
        conn.execute(
            """
            INSERT INTO ingest_state (
                id, fingerprint, embedding_model, embedding_dim, chunking_name,
                n_docs, n_chunks, n_embeddings, ingested_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                embedding_model = excluded.embedding_model,
                embedding_dim = excluded.embedding_dim,
                chunking_name = excluded.chunking_name,
                n_docs = excluded.n_docs,
                n_chunks = excluded.n_chunks,
                n_embeddings = excluded.n_embeddings,
                ingested_at = excluded.ingested_at
            """,
            (fingerprint, model, dim, chunking_name, len(docs), len(chunks), len(vectors), ingested_at),
        )

    usage = api_usage()
    return {
        "skipped": False,
        "reason": "ingested",
        "n_docs": len(docs),
        "n_chunks": len(chunks),
        "n_embeddings": len(vectors),
        "embedding_model": model,
        "embedding_dim": dim,
        "chunking_name": chunking_name,
        "chunking": params_dict(params, name=chunking_name),
        "fingerprint": fingerprint,
        "expected_docs": list(EXPECTED_DOCS),
        "ingested_at": ingested_at,
        **usage,
    }


def write_start_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
