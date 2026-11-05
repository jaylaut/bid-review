-- 首次初始化时自动执行（docker-entrypoint-initdb.d）
-- 注意：只在数据卷为空时执行。要重跑必须 `docker compose down -v`（会清空数据）。
--
-- 本文件只负责"扩展 + 中文分词配置"。
-- 业务表（documents / chunks / scoring_items / agent_state）见：
--   docs/03-RAG子系统详细架构.md §三    → documents / chunks / scoring_items
--   docs/07-Agent开发路线与阶段开发指引.md §A4 → agent_state
-- 那两张 DDL 建议用迁移工具或手工执行，不放在 initdb 里（initdb 只跑一次，不适合演进）。

-- ═══ 1. 扩展 ═══════════════════════════════════════════════════
CREATE EXTENSION IF NOT EXISTS vector;      -- 向量类型 + HNSW 索引
CREATE EXTENSION IF NOT EXISTS zhparser;    -- 中文分词（SCWS 驱动）

-- ═══ 2. 中文全文检索配置：zh ═══════════════════════════════════
-- docs/03 的 DDL 里写的是 to_tsvector('zh', content)，对应的就是这个配置。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'zh') THEN
        CREATE TEXT SEARCH CONFIGURATION zh (PARSER = zhparser);
    END IF;
END
$$;

-- 映射哪些词性参与索引
--   n=名词 v=动词 a=形容词 i=成语 e=叹词 l=习用语 j=简称略语 x=未知词
-- j 对招投标场景很重要：ISO27001 / SLA / DM8 这类缩写会落到"简称略语"或"未知"
DO $$
BEGIN
    BEGIN
        ALTER TEXT SEARCH CONFIGURATION zh
            ADD MAPPING FOR n, v, a, i, e, l, j, x WITH simple;
    EXCEPTION WHEN duplicate_object THEN
        RAISE NOTICE 'zh 分词映射已存在，跳过';
    END;
END
$$;

-- ═══ 3. 自检输出（构建/启动时在日志里应能看到正确分词）═════════
DO $$
DECLARE
    seg text;
    vec_version text;
BEGIN
    SELECT extversion INTO vec_version FROM pg_extension WHERE extname = 'vector';
    SELECT to_tsvector('zh', '支持国产数据库达梦DM8适配')
        INTO seg;
    RAISE NOTICE '[env-check] pgvector = %', vec_version;
    RAISE NOTICE '[env-check] 中文分词结果 = %', seg;
END
$$;
