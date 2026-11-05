"""双路并发召回（docs/03 §5.2）。权限过滤在 SQL 层强制注入，工具层绕不过。

【测试策略】真实 SQL 需要 PG + 向量数据；单测注入 fetch 函数，
验证的是"SQL 带了权限过滤、双路并发、RRF 正确衔接"——
SQL 本身的正确性由 PG 起来后的集成测试覆盖（test_db 的 skip 模式同款）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from common.context import AuthContext
from common.errors import PermissionDenied
from rag.online.fuse import POOL, rrf_fuse
from rag.online.types import Candidate, RetrieveQuery

# 注入点：测试替换；生产用 common.db.fetch_all
FetchFn = Callable[[str, tuple], Awaitable[list[Candidate]]]


def _acl_guard(q: RetrieveQuery, ctx: AuthContext) -> None:
    """入参侧权限自检。
    【why】防御性校验：调用方构造 RetrieveQuery 时带错 project_id
    （比如把用户 A 的查询配了用户 B 的项目），在这里拦下比返回空结果好——
    空结果会被当成"方案没写"，是错误结论而不是空页。"""
    if q.project_id != ctx.project_id:
        # 检索目标必须经 scoped_for 显式派生（专家跨项目评审的唯一合法路径）
        raise PermissionDenied(
            f"查询项目 {q.project_id} 与身份项目 {ctx.project_id} 不一致",
            details={"user": ctx.user_id},
        )


async def _fetch_dense(q: RetrieveQuery, fetch: FetchFn) -> list[Candidate]:
    # docs/03 §5.2 SQL_DENSE：HNSW 内积 + 权限过滤，ef_search 会话内动态可调
    # 【坑·已修】原写法 "SET LOCAL ...; SELECT ..." 两条语句拼一个字符串——
    # asyncpg 参数化查询（extended protocol）不允许多语句，真库直接报错。
    # 修正：set_config(..., true) 放 CTE，is_local=true 等价 SET LOCAL，
    # 单语句保持 prepared statement 可用。
    sql = """
    WITH _cfg AS (SELECT set_config('hnsw.ef_search', '128', true))
    SELECT c.chunk_id, c.doc_id, c.project_id, c.section_path, c.section_title,
           c.page_start, c.page_end, c.content, c.parent_id, c.chunk_kind,
           -(c.embedding <#> $1::vector) AS score
    FROM _cfg, chunks c
    WHERE c.project_id = $2 AND c.acl @> $3::jsonb AND c.chunk_kind <> 'caption'
    ORDER BY c.embedding <#> $1::vector
    LIMIT $4
    """
    rows = await fetch(sql, (q.embedding, q.project_id, _acl_json(q), 60))
    # 兼容两种返回：生产 asyncpg Record（dict 化）与测试注入的 Candidate
    return [r if isinstance(r, Candidate) else Candidate(**dict(r)) for r in rows]


async def _fetch_sparse(q: RetrieveQuery, fetch: FetchFn) -> list[Candidate]:
    # docs/03 §5.2 SQL_SPARSE：zhparser 全文（ts_rank_cd 走 BM25 语义）。
    # 评分项专有名词密集，稀疏路是刚需，权重实验留给 A8 单变量
    sql = """
    SELECT chunk_id, doc_id, project_id, section_path, section_title,
           page_start, page_end, content, parent_id, chunk_kind,
           ts_rank_cd(tsv, query, 32) AS score
    FROM chunks, plainto_tsquery('zh', $1) query
    WHERE project_id = $2 AND acl @> $3::jsonb AND tsv @@ query
    ORDER BY score DESC
    LIMIT $4
    """
    keywords = " ".join(q.keywords) if q.keywords else q.query_text
    rows = await fetch(sql, (keywords, q.project_id, _acl_json(q), 60))
    # 兼容两种返回：生产 asyncpg Record（dict 化）与测试注入的 Candidate
    return [r if isinstance(r, Candidate) else Candidate(**dict(r)) for r in rows]


async def retrieve(
    q: RetrieveQuery, ctx: AuthContext, *, fetch: FetchFn | None = None, pool: int = POOL
) -> list[Candidate]:
    """双路并发召回 + RRF 融合。延迟取决于慢的一路，不是两路之和。"""
    _acl_guard(q, ctx)
    if fetch is None:
        from common import db

        async def fetch(sql: str, args: tuple) -> list[Candidate]:
            return await db.fetch_all(sql, *args)
        # 【契约】FetchFn 是 (sql, args_tuple)；db.fetch_all 是 *args。
        # 不适配的话测试注入全过、生产路径全炸——本项目已实际踩过

    dense, sparse = await asyncio.gather(_fetch_dense(q, fetch), _fetch_sparse(q, fetch))
    return rrf_fuse(dense, sparse, pool=pool)


def _acl_json(q: RetrieveQuery) -> str:
    """acl list → jsonb 字符串。asyncpg 的 jsonb 参数吃字符串不吃 list；
    空 acl 兜底 proj 标签（空数组 @> 一切为 False 的静默失败）。"""
    import json
    items = q.acl or [f"proj:{q.project_id}"]
    return json.dumps(items, ensure_ascii=False)
