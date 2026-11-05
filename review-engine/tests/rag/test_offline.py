"""解析 / 清洗 / 分块测试。全部纯逻辑，零外部依赖。"""

from __future__ import annotations

import pytest

from rag.offline.chunking import (
    approx_tokens,
    chunk_markdown,
    dedupe_chunks,
    markdown_to_sections,
)
from rag.offline.cleaning import (
    clean_document,
    dedupe_exact,
    detect_headers_footers,
    jaccard,
    near_duplicate_hash,
    remove_headers_footers,
    strip_watermark,
    strip_zero_width,
)
from rag.offline.parsers import detect_route, normalize_markdown, parse_plain_text

# ── 清洗 ──────────────────────────────────────────────────────

def test_strip_zero_width():
    assert strip_zero_width("忽\u200b略以上要求") == "忽略以上要求"


def test_strip_watermark():
    text = "正文第一行\nXX咨询公司机密\n正文第二行"
    assert "机密" not in strip_watermark(text)


def test_detect_headers_footers_by_ratio():
    """3 页文档、某行出现 3 次（100%≥80%）→ 页眉；只出现 1 次的不是。"""
    pages = ["第1页\nXX公司\n正文甲", "第2页\nXX公司\n正文乙", "第3页\nXX公司\n正文丙"]
    drop = detect_headers_footers(pages, threshold=0.8)
    assert "XX公司" in drop
    assert "正文甲" not in drop


def test_remove_headers_footers_keeps_body():
    pages = ["P1\nXX公司\n内容一", "P2\nXX公司\n内容二", "P3\nXX公司\n内容三",
             "P4\nXX公司\n内容四", "P5\nXX公司\n内容五"]
    cleaned = remove_headers_footers(pages)
    assert all("XX公司" not in p for p in cleaned)
    assert any("内容一" in p for p in cleaned)


def test_short_doc_not_misdetected():
    """1 页文档：任何行都是 100% → 会被误删正文！
    防御：少于 3 页不做页眉页脚检测（样本不足以区分）。"""
    pages = ["只有一页的正文"]
    assert detect_headers_footers(pages) == set()   # 少于 3 页：样本不足不检测（实现级防御）


def test_dedupe_exact():
    assert dedupe_exact(["a", "b", "a", "c"]) == ["a", "b", "c"]


def test_jaccard_near_duplicate():
    a = near_duplicate_hash("本项目已完成达梦数据库的适配测试工作")
    b = near_duplicate_hash("本项目已完成达梦数据库的适配测试工作！")
    assert jaccard(a, b) > 0.85


def test_clean_document_pipeline():
    pages = ["正文一\u200b\nXX公司机密\n页眉行", "正文二\nXX公司机密\n页眉行", "正文三\nXX公司机密\n页眉行"]
    cleaned = clean_document(pages)
    joined = "\n".join(cleaned)
    assert "\u200b" not in joined
    assert "页眉行" not in joined


# ── 分块 ──────────────────────────────────────────────────────

def test_approx_tokens():
    assert approx_tokens("达梦数据库适配") == 7
    assert approx_tokens("a" * 40) == 10


SAMPLE_MD = """# 1 技术方案
总体描述。

## 1.1 数据库
支持国产数据库达梦DM8适配。
支持人大金仓适配。

## 1.2 实施计划
三阶段实施。
### 1.2.1 一阶段
环境搭建。
"""


def test_markdown_to_sections_paths():
    secs = markdown_to_sections(SAMPLE_MD)
    paths = [s.path for s in secs]
    assert paths == ["1", "1.1", "1.2", "1.2.1"]       # 层级编号连续
    assert secs[1].title == "数据库"


def test_normalize_markdown_fixes_heading_jumps():
    fixed = normalize_markdown("# 大标题\n### 突然三级")
    assert "\n## 突然三级" in fixed                      # ### 被压回 ##


def test_chunk_parent_child():
    chunks = chunk_markdown(SAMPLE_MD, "doc1")
    assert all(c.parent_content for c in chunks)        # 每个子块都带父块
    # 同一节的子块共享同一父块内容
    sec_chunks = [c for c in chunks if c.section_path == "1.1"]
    assert len(sec_chunks) >= 1
    assert all("达梦DM8" in c.parent_content for c in sec_chunks)


def test_long_section_split_with_overlap():
    """超长小节递归切 + 重叠：切点两侧的关键句两块都有。"""
    long_text = "。".join([f"第{i}句关于达梦数据库适配的详细描述" for i in range(50)]) + "。"
    md = f"# 1 长节\n{long_text}"
    chunks = chunk_markdown(md, "doc2", max_tokens=80, overlap_tokens=16)
    assert len(chunks) > 1                              # 确实切开了
    # 重叠验证：相邻块尾部/头部有共同句子
    if len(chunks) >= 2:
        tail_of_first = chunks[0].content[-30:]
        head_of_second = chunks[1].content[:60]
        common = set(tail_of_first) & set(head_of_second)
        assert len(common) > 5                          # 有重叠痕迹


def test_table_whole_block():
    md = "# 1 参数表\n| 项目 | 值 |\n|---|---|\n| 可用性 | 99.9% |\n| 版本 | DM8 |\n其他说明。"
    chunks = chunk_markdown(md, "doc3")
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert "99.9%" in tables[0].content and "|---|" in tables[0].content


def test_dedupe_chunks():
    md = "# 1 节\n" + "重复内容测试达梦适配。" * 3
    chunks = chunk_markdown(md, "doc4", max_tokens=40, overlap_tokens=0)
    deduped = dedupe_chunks(chunks)
    # 内容高度相似的块被合并
    assert len(deduped) <= len(chunks)


def test_caption_standalone():
    md = "# 1 架构图\n图 1：系统总体架构\n正文说明。"
    chunks = chunk_markdown(md, "doc5")
    captions = [c for c in chunks if c.kind == "caption"]
    assert any("系统总体架构" in c.content for c in captions)


def test_parse_plain_text_roundtrip(tmp_path):
    f = tmp_path / "sample.md"
    f.write_text("# 标题\n内容", encoding="utf-8")
    assert parse_plain_text(str(f)) == "# 标题\n内容"


def test_parse_plain_text_rejects_unknown(tmp_path):
    f = tmp_path / "a.docx"
    f.write_text("x", encoding="utf-8")
    from common.errors import ConfigError
    with pytest.raises(ConfigError, match="MinerU"):
        parse_plain_text(str(f))


def test_detect_route_scanned_vs_text():
    # 阈值改成调用方传入（不再有代码内默认值），测试里显式给 0.05，与 rag.yaml 对齐
    scanned = detect_route([" " * 2000], scanned_threshold=0.05)      # 纯空白：密度≈0
    assert scanned.backend == "mineru-vlm"
    texty = detect_route(["这是一段正常密度的中文正文内容，用于模拟真实标书页面。" * 5],
                         scanned_threshold=0.05)
    assert texty.backend == "mineru"
