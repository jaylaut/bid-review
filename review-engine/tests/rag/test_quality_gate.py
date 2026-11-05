"""质量门测试：阈值来自配置 + 乱码维度不误伤 markdown 语法。

重点是一条回归防线：旧实现 `[^\\w\\u4e00-\\u9fff]{3,}` 会把换行和 `#### ` 当成乱码，
导致一份完全干净的真实标书（data/parsed/tender_20260828.md）数出 1323 处"乱码"、
该维度恒判 0 分。下面的用例锁住"干净中文标书必须得 2 分"。
"""

from __future__ import annotations

from rag.offline.quality_gate import (
    QualityThresholds,
    _count_garbage_lines,
    _score_encoding,
    _score_page_text,
    _score_structure,
    score_parse_quality,
)


def _quality_gate_cfg(garbage_ratio: float = 0.3) -> dict:
    """构造一份和 config/rag.yaml 的 quality_gate 段同构的配置。"""
    return {
        "min_score": 6,
        "sample_rate": 0.05,
        "thresholds": {
            "encoding": {"replacement_warn": 5, "garbage_lines_warn": 5, "garbage_ratio": garbage_ratio},
            "length": {"min_chars_full": 2000, "min_chars_partial": 200},
            "structure": {"min_headers_full": 3, "min_headers_partial": 1},
            "watermark": {"residue_warn": 3},
        },
    }


# 一份"像真实标书"的干净 markdown：多级标题 + 表格分隔线 + 全角标点 + 破折号
CLEAN_MD = """# 第一章 招标公告
本项目为医疗信息化系统升级及运维项目，招标编号 1407812026CGK00146。

## 1.1 技术要求
| 序号 | 指标 | 要求 |
|---|---|---|
| 1 | 可用性 | ≥99.9% |
| 2 | 响应时间 | ≤200ms |

## 1.2 评分办法
技术方案（40 分）、商务（30 分）、价格（30 分）——三项合计 100 分……
""" * 14


def test_clean_markdown_gets_full_marks():
    """干净中文标书 → 编码维度 2 分。（旧实现在同一份文本上返回 0 分）"""
    th = QualityThresholds.from_mapping(_quality_gate_cfg())
    score, reason = _score_encoding(CLEAN_MD, th)
    assert score == 2, reason


def test_table_separator_not_garbage():
    """表格分隔线 / 等号线 / 连续中文标点都不是乱码；即使通篇只有这些符号也不扣分。"""
    md = "|---|---|---|\n| :--- | ---: |\n==========\n、、。\n#####\n"
    assert _count_garbage_lines(md, 0.3) == 0


def test_ocr_garbage_scores_zero():
    """替换符 + 私有区字符 + 乱码特征词成片出现 → 编码维度 0 分。"""
    md = ("\ue000\ue001\ue002\ue003" * 20 + "\ufffd" * 30 + "锟斤拷" * 10)
    th = QualityThresholds.from_mapping(_quality_gate_cfg())
    score, reason = _score_encoding(md, th)
    assert score == 0, reason


def test_thresholds_come_from_config():
    """阈值改一个数，结论就跟着变——证明分数真的由配置决定，不是代码里的常量。"""
    # 每行 3 个不可读字符 + 8 个汉字，占比 3/11≈0.27
    md = "\n".join(["\ue000\ue001\ue002正常文字说明内容"] * 6)

    # ratio=0.3：0.27 未过线 → 0 条乱码行 → 满分 2
    lenient = QualityThresholds.from_mapping(_quality_gate_cfg(garbage_ratio=0.3))
    assert lenient.garbage_ratio == 0.3
    assert _score_encoding(md, lenient)[0] == 2

    # ratio=0.2：同样的文本过线了 → 6 条乱码行 > 容忍线 5 → 0 分
    strict = QualityThresholds.from_mapping(_quality_gate_cfg(garbage_ratio=0.2))
    assert _score_encoding(md, strict)[0] == 0

    # 顺带验证其它维度也读配置：2 个标题不足 min_headers_full=3 → 1 分
    assert _score_structure("## 甲\n正文\n### 乙\n正文", lenient)[0] == 1


def test_from_mapping_missing_key_raises():
    """少配一个阈值必须当场炸，而不是静默用默认值。"""
    cfg = _quality_gate_cfg()
    del cfg["thresholds"]["encoding"]["garbage_ratio"]
    try:
        QualityThresholds.from_mapping(cfg)
    except KeyError as e:
        assert "garbage_ratio" in str(e)
    else:
        raise AssertionError("缺 garbage_ratio 应当抛 KeyError")


def test_watermark_residue_threshold_from_config():
    """水印残留的容忍处数也走配置：residue_warn=3 时 4 处判 0 分。"""
    th = QualityThresholds.from_mapping(_quality_gate_cfg())
    assert _score_page_text("本项目不含敏感词", th)[0] == 2
    assert _score_page_text("机密\n内部资料", th)[0] == 1      # 2 处 ≤ 3
    assert _score_page_text("机密" * 4, th)[0] == 0           # 4 处 > 3


def test_full_pipeline_clean_doc_passes():
    """整条链路：干净标书总分 8、不进复核队列。"""
    th = QualityThresholds.from_mapping(_quality_gate_cfg())
    report = score_parse_quality(CLEAN_MD, th)
    assert report.score == 8, report.reasons
    assert report.needs_review is False
