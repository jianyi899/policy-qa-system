#!/usr/bin/env python3
"""导入 70 篇语料，写入 documents 与 document_sections。

切分切片与 embedding 由 scripts/chunk_and_embed.py 负责，避免把默认切分与章节结构绑死。
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.corpus_io import load_corpus  # noqa: E402


def import_corpus() -> None:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    docs = load_corpus()

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            # chunk_embeddings 引用 chunks，需先清向量再清文档。
            cur.execute("DELETE FROM chunk_embeddings")
            cur.execute("DELETE FROM chunks")
            cur.execute("DELETE FROM document_sections")
            cur.execute("DELETE FROM documents")
            for doc in docs:
                cur.execute(
                    """
                    INSERT INTO documents (
                        doc_id, title, source_type, scope_type, scope_value,
                        effective_date, expire_date, body
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                    cur.execute(
                        """
                        INSERT INTO document_sections (
                            doc_id, section_path, heading, char_start, char_end, body
                        ) VALUES (%s, %s, %s, %s, %s, %s)
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
        conn.commit()

    counts: dict[str, int] = {}
    for doc in docs:
        counts[doc["source_type"]] = counts.get(doc["source_type"], 0) + 1
    print(f"imported {len(docs)} documents: {counts}")
    for doc in docs:
        print(f"  {doc['doc_id']} {doc['title']}")


if __name__ == "__main__":
    import_corpus()
