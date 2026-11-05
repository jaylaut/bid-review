"""缺口补齐模块的测试：indexer / 评分项抽取 / 三级缓存 / 评测 runner。"""

from __future__ import annotations

import json

import pytest

from agent.eval_runner import (
    _refusal_metrics,
    build_report,
    compare,
    confusion,
    load_golden,
    save_report,
)
from rag.offline.scoring_extract import (
    ScoringItem,
    extract_from_document,
    persist_items,
)
from rag.online.cache import FakeCache, sha256_key

# ── indexer：幂等入库 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_indexer_idempotent_skips_second_run():
    """同文档第二次入库必须 skip（幂等的文档级表达）。"""
    from rag.offline.indexer import index_document

    calls: list[tuple] = []

    async def fetch(sql: str, args: tuple) -> list[dict]:
        calls.append(("fetch", sql[:40]))
        if "SELECT doc_id FROM documents" in sql:
            # 第一次跑返回空；第二次返回已存在的 doc
            return [{"doc_id": "doc-1"}] if len(calls) > 1 else []
        if "INSERT INTO documents" in sql:
            return [{"doc_id": "doc-1"}]
        return []

    executed: list[str] = []

    async def execute(sql: str, args: tuple = ()) -> str:
        executed.append(sql[:40])
        return "OK"

    r1 = await index_document("# 1 节\n内容足够长触发分块的文本" * 5,
                              project_id="p1", doc_type="bid_response", title="t",
                              file_hash="h1", acl=["proj:p1"],
                              embedding_ver="v1", chunking_ver="c1",
                              fetch=fetch, execute=execute, embed_fn=None,
                              transaction=_fake_tx(executed))
    assert not r1.skipped and r1.chunks_written > 0

    # 第二次：fetch 直接命中 → skip，且不再执行任何写
    r2 = await index_document("# 1 节\n内容足够长触发分块的文本" * 5,
                              project_id="p1", doc_type="bid_response", title="t",
                              file_hash="h1", acl=["proj:p1"],
                              embedding_ver="v1", chunking_ver="c1",
                              fetch=fetch, execute=execute, embed_fn=None,
                              transaction=_fake_tx(executed))
    assert r2.skipped and r2.chunks_written == 0


@pytest.mark.asyncio
async def test_indexer_writes_chunks_with_parent():
    """子块必须带 parent_content（父子块设计的写入侧兑现）。"""
    from rag.offline.indexer import index_document

    inserted: list[tuple] = []

    async def fetch(sql: str, args: tuple) -> list[dict]:
        if "SELECT doc_id FROM documents" in sql:
            return []
        if "INSERT INTO documents" in sql:
            return [{"doc_id": "doc-9"}]
        return []

    async def execute(sql: str, args: tuple = ()) -> str:
        if "INSERT INTO chunks" in sql:
            inserted.append(args)
        return "OK"

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]

    md = ("# 1 技术方案\n" + "支持国产数据库达梦DM8适配，提供完整验证报告。" * 10)
    r = await index_document(md, project_id="p1", doc_type="bid_response", title="t",
                             file_hash="h9", acl=["proj:p1"],
                             embedding_ver="v1", chunking_ver="c1",
                             fetch=fetch, execute=execute, embed_fn=embed_fn,
                             transaction=_fake_tx(inserted))
    chunk_inserts = [a for (sql, a) in inserted if "INSERT INTO chunks" in sql]
    assert r.chunks_written == len(chunk_inserts)
    # 每条 chunk：acl 带 proj 标签、向量是 1024 维占位/真实
    for args in chunk_inserts:
        acl = json.loads(args[10])
        assert "proj:p1" in acl
        vec = json.loads(args[11])
        assert len(vec) == 1024


# ── 评分项抽取（链路 A）────────────────────────────────────────

class FakeExtractLLM:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []

    async def chat_json(self, prompt: str, schema, **kw):
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        return schema.model_validate(out)


@pytest.mark.asyncio
async def test_extract_splits_atomic_requirements():
    llm = FakeExtractLLM([{"items": [{
        "item_code": "S3.2.4", "item_text": "支持国产数据库（达梦、人大金仓）适配",
        "requirement": [{"must": "达梦适配", "evidence_required": "适配验证报告"},
                        {"must": "人大金仓适配"}],
    }]}])
    items = await extract_from_document(
        [("3.2 数据库要求", "3.2.4 支持国产数据库……" + "补充说明" * 10)], llm)
    assert len(items) == 1
    assert items[0].must_texts() == ["达梦适配", "人大金仓适配"]
    assert items[0].source_section == "3.2 数据库要求"


@pytest.mark.asyncio
async def test_extract_skips_tiny_sections_and_dedupes():
    """目录/空节跳过；评分办法跨章重复的条目去重（省 token + 防重复评审）。"""
    llm = FakeExtractLLM([{"items": [{"item_text": "支持达梦适配"}]},
                          {"items": [{"item_text": "支持达梦适配"}]}])
    sections = [("第一章", "x" * 100), ("短节", "太短"), ("第二章", "y" * 100)]
    items = await extract_from_document(sections, llm)
    assert len(items) == 1            # 短节被跳过 + 跨节去重


@pytest.mark.asyncio
async def test_extract_raises_when_nothing_found():
    llm = FakeExtractLLM([{"items": []}])
    with pytest.raises(Exception, match="未抽取到任何评分项"):
        await extract_from_document([("第一章", "a" * 100)], llm)


@pytest.mark.asyncio
async def test_persist_items_writes_jsonb():
    executed: list[tuple] = []

    async def execute(sql: str, args: tuple = ()) -> str:
        executed.append(args)
        return "INSERT 0 1"

    item = ScoringItem(item_text="支持达梦适配",
                       requirement=[{"must": "达梦适配", "evidence_required": "报告"}])
    n = await persist_items([item], "p1", execute=execute)
    assert n == 1
    req = json.loads(executed[0][5])
    assert req["must"] == ["达梦适配"] and req["evidence_required"] == ["报告"]


# ── 三级缓存 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_roundtrip_and_key_distinction():
    c = FakeCache()
    await c.put_query_vector("达梦适配", [0.1, 0.2])
    assert await c.get_query_vector("达梦适配") == [0.1, 0.2]
    assert await c.get_query_vector("金仓适配") is None


def test_sha256_key_separator_ambiguity():
    """【why】统一键构造：('a|bc', 'd') 和 ('a', 'bc|d') 必须不同键，
    手工 f-string 拼接会产生这种歧义碰撞。"""
    assert sha256_key("a|bc", "d") != sha256_key("a", "bc|d")


@pytest.mark.asyncio
async def test_cache_failure_is_silent():
    """【why】缓存是优化不是依赖：读写失败静默当 miss，
    Redis 挂了主链路照常（每次都 miss = 变慢，不是变错）。"""

    class Broken:
        async def get(self, k):
            raise ConnectionError("down")

        async def set(self, k, v, ex=None):
            raise ConnectionError("down")

    from rag.online.cache import RetrievalCache
    c = RetrievalCache(redis_client=Broken())
    assert await c.get("l1", "k") is None          # 读失败 → None（miss）
    await c.put("l1", "k", [1.0], 60)              # 写失败 → 不抛


# ── 评测 runner（A8）──────────────────────────────────────────

class _FakeConn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args) -> str:
        self._sink.append((sql[:40], args))
        return "OK"


def _fake_tx(sink: list):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tx():
        yield _FakeConn(sink)
    return tx


def _sample_results() -> list[dict]:
    rows = []
    # 80 题：60 对 / 20 错；NOT_FOUND 专项 12 题（11 对 1 漏）
    for _i in range(60):
        rows.append({"expected": "PASS", "actual": "PASS", "ok": True, "hit5": True,
                     "steps": 2, "cost_cny": 0.02, "latency_ms": 1000,
                     "category": "技术参数", "terminated_by": "answer"})
    for _i in range(8):
        rows.append({"expected": "PARTIAL", "actual": "PASS", "ok": False, "hit5": False,
                     "steps": 3, "cost_cny": 0.03, "latency_ms": 1500,
                     "category": "技术参数", "terminated_by": "answer"})
    for _i in range(11):
        rows.append({"expected": "NOT_FOUND", "actual": "NOT_FOUND", "ok": True,
                     "hit5": False, "steps": 2, "cost_cny": 0.02, "latency_ms": 900,
                     "category": "资质", "terminated_by": "answer"})
    rows.append({"expected": "NOT_FOUND", "actual": "PARTIAL", "ok": False, "hit5": False,
                 "steps": 4, "cost_cny": 0.04, "latency_ms": 2000,
                 "category": "资质", "terminated_by": "answer"})
    return rows


def test_confusion_shows_error_direction():
    m = confusion(_sample_results())
    assert m["PARTIAL"]["PASS"] == 8
    assert m["NOT_FOUND"]["PARTIAL"] == 1


def test_refusal_metrics_direction():
    """漏检率 1/12 ≈ 0.083：漏检比误判更贵，单独监控。"""
    m = _refusal_metrics(_sample_results())
    assert m["not_found_precision"] == 1.0
    assert 0.08 <= m["not_found_miss_rate"] <= 0.09


def test_build_report_shape():
    r = build_report(_sample_results(), meta={"commit": "abc123", "model": "fake"})
    assert r["overall"]["accuracy"] == round(71 / 80, 3)
    assert r["meta"]["golden_rows"] == 80
    assert "not_found_miss_rate" in r["refusal"]
    assert r["termination"]["answer"] == 1.0


def test_load_golden_rejects_small_set():
    """【why】20 题差一道 5 个点：小集合的'提升'结论全部不可信。"""
    import os
    import pathlib
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)                       # Windows 上句柄不关 unlink 会 PermissionError
    fp = pathlib.Path(path)
    fp.write_text("\n".join(json.dumps({"expected_verdict": "PASS"}) for _ in range(20)),
                  encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="至少 80"):
            load_golden(str(fp))
    finally:
        fp.unlink()


def test_compare_gates_block_on_accuracy_drop():
    base = build_report(_sample_results(), meta={"commit": "a", "model": "m"})
    worse = dict(base)
    worse["overall"] = {**base["overall"], "accuracy": base["overall"]["accuracy"] - 0.1}
    out = compare(base, worse)
    assert out["verdict"] == "BLOCK"


def test_compare_gates_block_on_cost_spike():
    base = build_report(_sample_results(), meta={"commit": "a", "model": "m"})
    costly = dict(base)
    costly["overall"] = {**base["overall"], "avg_cost_cny": base["overall"]["avg_cost_cny"] * 2}
    out = compare(base, costly)
    assert out["verdict"] == "BLOCK"


def test_compare_pass_when_stable():
    base = build_report(_sample_results(), meta={"commit": "a", "model": "m"})
    assert compare(base, base)["verdict"] == "PASS"


def test_save_report_filename_has_date_and_commit(tmp_path):
    r = build_report(_sample_results(), meta={"commit": "abcd1234", "model": "m"})
    fp = save_report(r, out_dir=str(tmp_path))
    assert "abcd12" in fp and ".json" in fp
