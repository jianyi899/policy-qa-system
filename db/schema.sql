-- 轻量版 SQLite 表结构。向量表由 app/db.py 按配置中的 embedding 维度创建，不在此写死维度。
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id             TEXT UNIQUE,
    display_name            TEXT,
    email                   TEXT,
    is_member               INTEGER NOT NULL DEFAULT 0,
    membership_tier         TEXT NOT NULL DEFAULT 'none',
    membership_started_at   TEXT,
    membership_expires_at   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no                TEXT NOT NULL UNIQUE,
    user_id                 INTEGER NOT NULL REFERENCES users(id),
    sku                     TEXT NOT NULL,
    category                TEXT NOT NULL,
    shipping_insurance      INTEGER NOT NULL DEFAULT 0,
    received_at             TEXT,
    order_status            TEXT NOT NULL DEFAULT 'created',
    amount_cents            INTEGER,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS logistics (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id                INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tracking_no             TEXT NOT NULL UNIQUE,
    carrier                 TEXT,
    status                  TEXT NOT NULL,
    shipped_at              TEXT,
    delivered_at            TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    scope_type      TEXT NOT NULL,
    scope_value     TEXT,
    effective_date  TEXT NOT NULL,
    expire_date     TEXT,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (source_type IN ('policy', 'manual', 'faq', 'noise')),
    CHECK (scope_type IN ('sku', 'category', 'general'))
);

CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    section_path    TEXT NOT NULL,
    heading         TEXT,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    body            TEXT NOT NULL,
    UNIQUE (doc_id, section_path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    section_path    TEXT NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    UNIQUE (doc_id, section_path, char_start, char_end)
);

CREATE TABLE IF NOT EXISTS ingest_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint         TEXT NOT NULL,
    embedding_model     TEXT NOT NULL,
    embedding_dim       INTEGER NOT NULL,
    chunking_name       TEXT NOT NULL,
    n_docs              INTEGER NOT NULL,
    n_chunks            INTEGER NOT NULL,
    n_embeddings        INTEGER NOT NULL,
    ingested_at         TEXT NOT NULL
);
