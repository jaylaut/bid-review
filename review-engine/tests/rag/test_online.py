"""RRF 融合 / 双路召回 / 上下文组装。全部纯逻辑或注入 fake，零网络零 DB。"""

from __future__ import annotations

import json

import pytest

from common.context import AuthContext
from common.errors import PermissionDenied
from models.types import Hit
from rag.online.assemble import (
    EvidenceBlock,
    approx_tokens,
    assemble_context,
    label_blocks,
    u_shape_order,
)
from rag.online.fuse import rrf_fuse
from rag.online.retrieve import retrieve
from rag.online.types import Candidate, RetrieveQuery


def _c(chunk_id: str, content: str = "x", parent: str | None = None,
       parent_content: str | None = None) -> Candidate:
    return Candidate(
        chunk_id=chunk_id, doc_id="d1", project_id="p1",
        section_path="3.2.4", page_start=87, content=content,
        parent_id=parent, parent_content=parent_content,
    )


# ── RRF ───────────────────────────────────────────────────────

def test_rrf_both_hit_beats_single_hit():
    """两路都命中的 chunk 应排最前：1/61+1/61 > 1/61。"""
    both = _c("both")
    dense_only = _c("dense_only")
    out = rrf_fuse([both, dense_only], [both, _c("sparse_only")])
    assert out[0].chunk_id == "both"
    assert out[0].dense_rank == 1 and out[0].sparse_rank == 1


def test_rrf_score_math():
    """手算验证：k=60，rank1 单路 = 1/61 ≈ 0.01639。"""
    out = rrf_fuse([_c("a")], [])
    assert abs(out[0].score - 1 / 61) < 1e-9


def test_rrf_pool_truncates():
    out = rrf_fuse([_c(f"d{i}") for i in range(80)], [], pool=50)
    assert len(out) == 50


def test_rrf_empty_input():
    assert rrf_fuse([], []) == []


# ── retrieve ──────────────────────────────────────────────────

def _q(project_id: str = "p1", acl: tuple = ("proj:p1",)) -> RetrieveQuery:
    return RetrieveQuery(item_id="S1", query_text="达梦适配", keywords=["达梦"],
                         embedding=[0.1] * 1024, project_id=project_id, acl=list(acl))


@pytest.mark.asyncio
async def test_retrieve_merges_two_ways():
    """fetch 按 SQL 内容分流：含 embedding <#> 的是稠密路，ts_rank 是稀疏路。"""

    async def fake_fetch(sql: str, args: tuple) -> list[Candidate]:
        if "<#>" in sql:
            assert args[1] == "p1"                       # project_id 过滤在 SQL 里
            assert json.loads(args[2]) == ["proj:p1"]    # acl 过滤在 SQL 里
            return [_c("c1"), _c("c2")]
        assert "ts_rank_cd" in sql
        return [_c("c2"), _c("c3")]

    out = await retrieve(_q(), AuthContext("u1", "p1", acl=("proj:p1",)), fetch=fake_fetch)
    ids = [c.chunk_id for c in out]
    assert ids == ["c2", "c1", "c3"]    # c2 两路命中排第一
    assert len(out) == 3


@pytest.mark.asyncio
async def test_retrieve_rejects_project_mismatch():
    """【why】查询项目 ≠ 身份项目必须当场炸：返回空会被当成'方案没写'，
    是错误结论而不是空页（retrieve._acl_guard 的注释同款理由）。"""
    with pytest.raises(PermissionDenied):
        await retrieve(_q(project_id="pX"), AuthContext("u1", "p1", acl=("proj:p1",)),
                       fetch=_noop)


async def _noop(sql: str, args: tuple) -> list[Candidate]:  # pragma: no cover
    return []


@pytest.mark.asyncio
async def test_retrieve_wraps_acl_for_sql():
    """acl 元组 → SQL 的 jsonb 字符串。格式错 = 过滤恒空（静默失败）。"""

    async def fake_fetch(sql: str, args: tuple) -> list[Candidate]:
        assert json.loads(args[2]) == ["proj:p1", "sec:标段1"]
        return []

    await retrieve(_q(acl=("proj:p1", "sec:标段1")),
                   AuthContext("u1", "p1", acl=("proj:p1", "sec:标段1")), fetch=fake_fetch)


# ── 组装 ──────────────────────────────────────────────────────

def test_approx_tokens_cjk_vs_latin():
    assert approx_tokens("达梦数据库适配") == 7          # 每字 1 token（7 个字）
    assert approx_tokens("abcd" * 8) == 8               # 32 字符 / 4
    assert approx_tokens("达梦 dm8") == 3               # 2 CJK + 4 latin//4


def test_u_shape_puts_important_at_ends():
    blocks = [EvidenceBlock(f"c{i}", "d", 1, "s", f"内容{i}", f"S{i}") for i in range(4)]
    ordered = u_shape_order(blocks, budget_tokens=10_000)
    # 0→头, 1→尾, 2→头, 3→尾(反转后紧跟头侧)：头 [0,2] + 尾反转 [3,1]
    assert [b.chunk_id for b in ordered] == ["c0", "c2", "c3", "c1"]


def test_u_shape_respects_budget():
    long_blocks = [EvidenceBlock(f"c{i}", "d", 1, "s", "长" * 3000, f"S{i}") for i in range(5)]
    ordered = u_shape_order(long_blocks, budget_tokens=3200)
    assert 0 < len(ordered) < 5                          # 有裁剪


@pytest.mark.asyncio
async def test_assemble_prefers_parent_content():
    hits = [
        Hit(chunk_id="c1", doc_id="投标文件.docx", project_id="p1", section_path="3.2.4",
            page_start=87, content="子块片段", parent_id="p1c",
            parent_content="整节完整内容，含达梦与金仓对比"),
    ]
    blocks = await assemble_context(hits)
    assert blocks[0].content.startswith("整节完整内容")    # 父块优先，一次 DB 往返都不用


@pytest.mark.asyncio
async def test_assemble_falls_back_to_parent_lookup():
    hits = [Hit(chunk_id="c1", doc_id="d", project_id="p1", section_path="3.2",
                page_start=1, content="子块", parent_id="pid")]

    async def lookup(parent_id: str) -> str:
        assert parent_id == "pid"
        return "回查到的父块内容"

    blocks = await assemble_context(hits, parent_lookup=lookup)
    assert blocks[0].content == "回查到的父块内容"


@pytest.mark.asyncio
async def test_assemble_dedupes_same_parent():
    hits = [
        Hit(chunk_id="c1", doc_id="d", project_id="p", section_path="3.2",
            page_start=1, content="片段1", parent_id="P"),
        Hit(chunk_id="c2", doc_id="d", project_id="p", section_path="3.2",
            page_start=1, content="片段2", parent_id="P"),   # 同父块兄弟
    ]
    blocks = await assemble_context(hits)
    assert len(blocks) == 1                                   # 只保留一条


def test_label_blocks_format():
    b = EvidenceBlock("c1", "投标文件.docx", 87, "3.2.4", "已完成达梦适配", "S1")
    text = label_blocks([b])
    assert "[S1] 投标文件.docx 第87页 3.2.4：" in text
    assert text.endswith("[/S1]")                             # 闭标签：A6 回查按它切


@pytest.mark.asyncio
async def test_labels_are_sequential_after_assemble():
    """U 型排序后 label 必须按最终顺序连续重排——A6 证据回查按 label 对齐，
    错位等于把证据张冠李戴。"""
    hits = [Hit(chunk_id=f"c{i}", doc_id="d", project_id="p", section_path="s",
                page_start=i, content=f"内容{i}") for i in range(4)]
    blocks = await assemble_context(hits)
    assert [b.label for b in blocks] == ["S1", "S2", "S3", "S4"]
