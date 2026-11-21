"""密集检索（sqlite-vec 余弦距离）。B0 只用本模块；BM25、RRF 与 rerank 留给后续阶段。"""

from __future__ import annotations

from app.db import VECTOR_TABLE, pack_embedding


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
