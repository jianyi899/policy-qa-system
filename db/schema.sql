-- 政策问答系统第一段地基表结构。
-- embedding 列维度不得写死为 1024。
-- 本文件中的 :embedding_dim 由 db/migrate.py 按环境变量 EMBEDDING_DIM 替换为实际正整数。
-- 若尚未锁定 E0（未提供合法正整数维度），则只建业务表与文档表，暂不创建 chunk_embeddings。

CREATE TABLE IF NOT EXISTS users (
    id                      BIGSERIAL PRIMARY KEY,
    external_id             TEXT UNIQUE,
    display_name            TEXT,
    email                   TEXT,
    is_member               BOOLEAN NOT NULL DEFAULT FALSE,
    membership_tier         TEXT NOT NULL DEFAULT 'none',
    membership_started_at   TIMESTAMPTZ,
    membership_expires_at   TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN users.is_member IS '是否会员；后续查订单需能返回该字段';
COMMENT ON COLUMN users.membership_tier IS '会员档位，如 none / regular / plus';

CREATE TABLE IF NOT EXISTS orders (
    id                      BIGSERIAL PRIMARY KEY,
    order_no                TEXT NOT NULL UNIQUE,
    user_id                 BIGINT NOT NULL REFERENCES users(id),
    sku                     TEXT NOT NULL,
    category                TEXT NOT NULL,
    shipping_insurance      BOOLEAN NOT NULL DEFAULT FALSE,
    received_at             TIMESTAMPTZ,
    order_status            TEXT NOT NULL DEFAULT 'created',
    amount_cents            INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN orders.sku IS '下单 SKU';
COMMENT ON COLUMN orders.category IS '商品类目';
COMMENT ON COLUMN orders.shipping_insurance IS '是否购买运费险';
COMMENT ON COLUMN orders.received_at IS '签收时间';

CREATE TABLE IF NOT EXISTS refunds (
    id                      BIGSERIAL PRIMARY KEY,
    refund_no               TEXT NOT NULL UNIQUE,
    order_id                BIGINT NOT NULL REFERENCES orders(id),
    status                  TEXT NOT NULL,
    reason                  TEXT,
    amount_cents            INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT refunds_status_check
        CHECK (status IN ('pending', 'processing', 'approved', 'rejected', 'cancelled', 'completed'))
);

COMMENT ON COLUMN refunds.reason IS '退款理由，不作为幂等键的一部分';
COMMENT ON COLUMN refunds.created_at IS '申请时间，日期不作为幂等键的一部分';

-- 同一订单只能有一笔进行中的退款。进行中定义为 pending / processing。
-- 幂等仅约束「进行中」状态，不把当日和理由写入唯一键。
CREATE UNIQUE INDEX IF NOT EXISTS ux_refunds_one_open_per_order
    ON refunds (order_id)
    WHERE status IN ('pending', 'processing');

CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    scope_type      TEXT NOT NULL,
    scope_value     TEXT,
    effective_date  DATE NOT NULL,
    expire_date     DATE,
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT documents_source_type_check
        CHECK (source_type IN ('policy', 'manual', 'faq', 'noise')),
    CONSTRAINT documents_scope_type_check
        CHECK (scope_type IN ('sku', 'category', 'general'))
);

COMMENT ON TABLE documents IS '语料文档；标注键使用 doc_id + section_path + 字符起止，不直接使用 chunk_id。source_type=noise 为干扰语料，不得覆盖正式政策答案';

CREATE TABLE IF NOT EXISTS document_sections (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    section_path    TEXT NOT NULL,
    heading         TEXT,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    body            TEXT NOT NULL,
    UNIQUE (doc_id, section_path)
);

COMMENT ON COLUMN document_sections.section_path IS '稳定章节路径，如 1.适用范围；与字符起止一起作为后续标注映射键';
COMMENT ON COLUMN document_sections.char_start IS '相对 documents.body 的起始字符偏移（含）';
COMMENT ON COLUMN document_sections.char_end IS '相对 documents.body 的结束字符偏移（不含）';


-- 退款幂等键：只用于识别同一次提交的重试，不得包含申请日期或理由。
ALTER TABLE refunds ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_refunds_idempotency_key
    ON refunds (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
COMMENT ON COLUMN refunds.idempotency_key IS '幂等键；不得包含申请当日日期或退款理由';

-- 物流：仅存本库数据，不对接外部物流 API。订单删除时级联清理，避免影响既有地基验证脚本。
CREATE TABLE IF NOT EXISTS shipments (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tracking_no     TEXT NOT NULL UNIQUE,
    carrier         TEXT,
    status          TEXT NOT NULL,
    shipped_at      TIMESTAMPTZ,
    delivered_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shipment_events (
    id              BIGSERIAL PRIMARY KEY,
    shipment_id     BIGINT NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    event_time      TIMESTAMPTZ NOT NULL,
    location        TEXT,
    status          TEXT NOT NULL,
    description     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE shipments IS '本库物流运单；查物流工具只读本表';
COMMENT ON TABLE shipment_events IS '物流轨迹事件，按 event_time 排序';

-- 日报快照。查询动作只读实时聚合，不写本表；显式 generate 才按自然日（Asia/Shanghai）幂等写入。
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date         DATE PRIMARY KEY,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    orders_received     INTEGER NOT NULL DEFAULT 0,
    refunds_created     INTEGER NOT NULL DEFAULT 0,
    refunds_open        INTEGER NOT NULL DEFAULT 0,
    refunds_completed   INTEGER NOT NULL DEFAULT 0,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE daily_reports IS '日报快照，按 Asia/Shanghai 的日历日唯一；查询类调用不得写入';

-- 切分结果。chunk_id 仅用于存储与检索，评测标注不得直接使用 chunk_id。
CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    section_path    TEXT NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    UNIQUE (doc_id, section_path, char_start, char_end)
);

-- 以下语句仅在 EMBEDDING_DIM 已提供时由 migrate.py 执行。
-- BEGIN_EMBEDDING_TABLE
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    chunk_id        BIGINT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model_name      TEXT NOT NULL,
    dim             INTEGER NOT NULL,
    embedding       vector(__EMBEDDING_DIM__) NOT NULL
);
-- END_EMBEDDING_TABLE
