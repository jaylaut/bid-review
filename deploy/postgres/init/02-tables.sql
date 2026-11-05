-- 业务表 DDL（docs/03 §3.2/§3.3 + docs/07 §A4）
-- 执行方式（initdb 已跑过，手工执行）：
--   docker exec -i bid-pg psql -U bid -d bid_review < deploy/postgres/init/02-tables.sql
-- 与 docs/03 §3.2 的差异：
--   chunks 增加 status 列（§4.5 软删策略需要，原 DDL 缺失）
--   chunks 增加 page_end（引用定位区间）
--   embedding 参数通过 pgvector codec 传，无需 cast

-- ═══ 1. documents ════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS documents (
    doc_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      VARCHAR(64)  NOT NULL,
    doc_type        VARCHAR(32)  NOT NULL,          -- bid_doc | bid_response | reference
    title           TEXT,
    file_sha256     CHAR(64)     NOT NULL,
    file_uri        TEXT         NOT NULL DEFAULT '',
    parse_backend   VARCHAR(32),
    parse_quality   SMALLINT,
    acl             JSONB        NOT NULL,
    status          VARCHAR(24)  NOT NULL DEFAULT 'uploaded',
    embedding_ver   VARCHAR(64)  NOT NULL,
    chunking_ver    VARCHAR(32)  NOT NULL,
    created_by      VARCHAR(64),
    created_at      TIMESTAMPTZ  DEFAULT now(),
    UNIQUE (project_id, file_sha256)
);
CREATE INDEX IF NOT EXISTS idx_doc_project ON documents(project_id, status);

-- ═══ 2. chunks ═══════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id          UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    project_id      VARCHAR(64) NOT NULL,
    parent_id       UUID,
    section_path    TEXT,
    section_title   TEXT,
    page_start      SMALLINT,
    page_end        SMALLINT,
    content         TEXT NOT NULL,
    parent_content  TEXT,
    token_count     SMALLINT,
    chunk_kind      VARCHAR(16) DEFAULT 'text',      -- text|table|caption
    acl             JSONB NOT NULL,
    status          VARCHAR(16) DEFAULT 'active',    -- active | deleted（§4.5 软删）
    embedding       vector(1024) NOT NULL,
    tsv             tsvector,                          -- 触发器维护（见下）
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- 三路索引（docs/03 §3.2）——缺 HNSW 是 pgvector 最经典的静默全表扫
CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks
    USING hnsw (embedding vector_ip_ops)
    WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING gin (tsv);

CREATE OR REPLACE FUNCTION chunks_tsv_trigger() RETURNS trigger AS $$
BEGIN
    NEW.tsv := to_tsvector('zh'::regconfig, NEW.content);
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_chunks_tsv ON chunks;
CREATE TRIGGER trg_chunks_tsv
    BEFORE INSERT OR UPDATE OF content ON chunks
    FOR EACH ROW EXECUTE FUNCTION chunks_tsv_trigger();
CREATE INDEX IF NOT EXISTS idx_chunks_filter ON chunks (project_id, doc_id, chunk_kind);
CREATE INDEX IF NOT EXISTS idx_chunks_acl ON chunks USING gin (acl jsonb_path_ops);

-- ═══ 3. scoring_items ════════════════════════════════════════
CREATE TABLE IF NOT EXISTS scoring_items (
    item_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      VARCHAR(64) NOT NULL,
    item_code       VARCHAR(32),
    category        VARCHAR(64),
    item_text       TEXT NOT NULL,
    weight          NUMERIC(5,2),
    requirement     JSONB,                            -- {must:[], evidence_required:[]}
    source_ref      JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_project ON scoring_items(project_id);

-- ═══ 4. agent_state（docs/07 §A4）════════════════════════════
CREATE TABLE IF NOT EXISTS agent_state (
    task_id      VARCHAR(64) NOT NULL,
    item_id      VARCHAR(64) NOT NULL,
    step         SMALLINT    NOT NULL,
    status       VARCHAR(16) NOT NULL,   -- running | done | pending_human | failed
    state_json   JSONB       NOT NULL,
    state_hash   CHAR(64)    NOT NULL,
    schema_ver   SMALLINT    NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (task_id, item_id, step)
);
CREATE INDEX IF NOT EXISTS idx_agent_state_resume ON agent_state (task_id, status, step DESC);
CREATE INDEX IF NOT EXISTS idx_agent_state_gc ON agent_state (created_at);
