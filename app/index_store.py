"""把切分与向量写入（或读出）Postgres。每次替换当前索引中的切片集。"""

from __future__ import annotations

import psycopg

from app.embeddings import LOCKED_DIM, LOCKED_MODEL


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(format(x, ".8g") for x in vec) + "]"


def parse_vector_literal(lit: str) -> list[float]:
    s = str(lit).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return []
    return [float(x) for x in s.split(",")]


def replace_chunks(conn, chunks: list[dict], vectors: list[list[float]]) -> list[int]:
    if len(chunks) != len(vectors):
        raise ValueError("chunks 与 vectors 数量不一致")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunk_embeddings")
        cur.execute("DELETE FROM chunks")
        chunk_ids: list[int] = []
        for ch in chunks:
            cur.execute(
                """
                INSERT INTO chunks (doc_id, section_path, char_start, char_end, text)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"], ch["text"]),
            )
            chunk_ids.append(int(cur.fetchone()[0]))
        for chunk_id, vec in zip(chunk_ids, vectors, strict=True):
            if len(vec) != LOCKED_DIM:
                raise ValueError("embedding dim mismatch")
            cur.execute(
                """
                INSERT INTO chunk_embeddings (chunk_id, model_name, dim, embedding)
                VALUES (%s, %s, %s, %s::vector)
                """,
                (chunk_id, LOCKED_MODEL, LOCKED_DIM, vector_literal(vec)),
            )
    conn.commit()
    return chunk_ids


def load_chunks_with_embeddings(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.doc_id, c.section_path, c.char_start, c.char_end, c.text,
                   e.model_name, e.dim, e.embedding::text
            FROM chunks c
            JOIN chunk_embeddings e ON e.chunk_id = c.id
            ORDER BY c.doc_id, c.section_path, c.char_start, c.id
            """
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "id": r[0],
                "doc_id": r[1],
                "section_path": r[2],
                "char_start": r[3],
                "char_end": r[4],
                "text": r[5],
                "model_name": r[6],
                "dim": r[7],
                "embedding": parse_vector_literal(r[8]),
            }
        )
    return items


def dump_embeddings_for_cache(conn) -> list[tuple[str, list[float]]]:
    items = load_chunks_with_embeddings(conn)
    return [(it["text"], it["embedding"]) for it in items]
