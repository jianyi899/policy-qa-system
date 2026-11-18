#!/usr/bin/env python3
"""初始化或升级数据库表结构。向量列维度从 EMBEDDING_DIM 读取，不硬编码 1024。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import ClientCursor
from dotenv import load_dotenv

# 保证可以从仓库根目录直接运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _split_embedding_block(sql: str) -> tuple[str, str]:
    start = sql.find("-- BEGIN_EMBEDDING_TABLE")
    end = sql.find("-- END_EMBEDDING_TABLE")
    if start < 0 or end < 0:
        raise RuntimeError("schema.sql 缺少 embedding 表标记")
    base = sql[:start]
    embed = sql[start:end]
    return base, embed


def migrate() -> None:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    base_sql, embed_sql = _split_embedding_block(sql)

    dim_raw = os.environ.get("EMBEDDING_DIM") or (
        str(settings.embedding_dim) if settings.embedding_dim else ""
    )
    dim: int | None
    if dim_raw.strip():
        dim = int(dim_raw)
        if dim <= 0:
            raise ValueError("EMBEDDING_DIM 必须是正整数")
    else:
        dim = None

    with psycopg.connect(settings.dsn(), cursor_factory=ClientCursor) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(base_sql)
        # 已有库的 CHECK 不会被 CREATE TABLE IF NOT EXISTS 更新，需显式对齐。
        conn.execute(
            "ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_type_check"
        )
        conn.execute(
            """
            ALTER TABLE documents
            ADD CONSTRAINT documents_source_type_check
            CHECK (source_type IN ('policy', 'manual', 'faq', 'noise'))
            """
        )
        if dim is not None:
            conn.execute(embed_sql.replace("__EMBEDDING_DIM__", str(dim)))
            print(f"schema applied; chunk_embeddings.embedding vector({dim})")
        else:
            print(
                "schema applied; EMBEDDING_DIM unset, skipped chunk_embeddings "
                "(E0 not locked yet)"
            )
        conn.commit()


if __name__ == "__main__":
    migrate()
