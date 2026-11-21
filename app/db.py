"""SQLite + sqlite-vec 连接、建表与健康检查。不连接 Postgres 或 Redis。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec
from sqlite_vec import serialize_float32

from app.config import Settings, get_settings, repo_root

SCHEMA_PATH = repo_root() / "db" / "schema.sql"
BUSINESS_TABLES = (
    "users",
    "orders",
    "logistics",
    "documents",
    "sections",
    "chunks",
    "ingest_state",
)
VECTOR_TABLE = "chunk_embeddings"


def _require_positive_dim(dim: int) -> int:
    value = int(dim)
    if value <= 0:
        raise ValueError("EMBEDDING_DIM 必须是正整数，且从配置读取")
    return value


def connect(settings: Settings | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    if settings is None:
        settings = get_settings()
    path = settings.db_path()
    if read_only and not path.is_file():
        raise FileNotFoundError(f"数据库不存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = path.resolve().as_uri()
    if read_only:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@contextmanager
def connection(*, read_only: bool = False, settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(settings, read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        if not read_only:
            conn.rollback()
        raise
    finally:
        conn.close()


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _vector_table_dim(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (VECTOR_TABLE,),
    ).fetchone()
    if not row or not row[0]:
        return None
    sql = str(row[0])
    marker = "float["
    start = sql.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = sql.find("]", start)
    if end < 0:
        return None
    try:
        return int(sql[start:end])
    except ValueError:
        return None


def _vector_table_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (VECTOR_TABLE,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def ensure_vector_table(conn: sqlite3.Connection, dim: int) -> None:
    dim = _require_positive_dim(dim)
    # 维度来自配置，不能写死为 1024。距离使用余弦，与文本 embedding 的常用比较方式一致。
    expected_sql = (
        f"CREATE VIRTUAL TABLE {VECTOR_TABLE} "
        f"USING vec0(chunk_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
    )
    current_sql = _vector_table_sql(conn)
    if current_sql == expected_sql:
        return
    if current_sql is not None:
        conn.execute(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
    conn.execute(expected_sql)


def init_db(settings: Settings | None = None) -> Path:
    if settings is None:
        settings = get_settings()
    dim = _require_positive_dim(settings.embedding_dim)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connection(settings=settings) as conn:
        conn.executescript(schema)
        ensure_vector_table(conn, dim)
    return settings.db_path()


def pack_embedding(vec: list[float]) -> bytes:
    return serialize_float32(vec)


def table_names(conn: sqlite3.Connection) -> list[str]:
    names = _existing_tables(conn)
    ordered = [t for t in (*BUSINESS_TABLES, VECTOR_TABLE) if t in names]
    extras = sorted(n for n in names if n not in ordered and not n.startswith("sqlite_") and not n.startswith("chunk_embeddings_"))
    return ordered + extras


def check_sqlite(settings: Settings | None = None) -> dict:
    if settings is None:
        settings = get_settings()
    path = settings.db_path()
    if not path.is_file():
        return {"ok": False, "detail": "db_missing", "path": str(path)}
    try:
        with connection(read_only=True, settings=settings) as conn:
            version = conn.execute("SELECT vec_version()").fetchone()[0]
            names = table_names(conn)
            missing = [t for t in (*BUSINESS_TABLES, VECTOR_TABLE) if t not in _existing_tables(conn)]
            dim = _vector_table_dim(conn)
            n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] if "documents" in names else 0
            n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] if "chunks" in names else 0
            n_vecs = (
                conn.execute(f"SELECT COUNT(*) FROM {VECTOR_TABLE}").fetchone()[0]
                if VECTOR_TABLE in _existing_tables(conn)
                else 0
            )
        ok = not missing
        return {
            "ok": ok,
            "detail": "ok" if ok else f"missing_tables:{','.join(missing)}",
            "path": str(path),
            "vec_version": version,
            "vec_dim": dim,
            "tables": names,
            "n_docs": int(n_docs),
            "n_chunks": int(n_chunks),
            "n_embeddings": int(n_vecs),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": type(exc).__name__, "path": str(path)}
