"""解析阶段的数据契约和配置结构。

判断标准：会被两个以上模块用到的东西放这里。流水线自己用的中间变量不要往这塞，
否则这个文件会变成杂物间。

三个 dataclass 都是 frozen。任务、结果、配置在构造之后都不该被改：
批量跑的时候被某处改掉一个字段，同一批里前后两份文档用的就不是同一套规则，出问题没法复现。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag.offline.quality_gate import QualityThresholds
from rag.offline.source import SourcePolicy

__all__ = ["FAILED", "PARSED", "SKIPPED", "ParseConfig", "ParseOutcome", "ParseTask"]

# 三种状态。写成常量是为了避免 "skip" / "skipped" 这种拼错——
# 拼错了统计数字对不上，但不会报错，只能靠肉眼数日志。
PARSED = "parsed"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class ParseTask:
    """一个待解析任务，单位是项目目录，不是单个文件。

    一个项目目录里通常有主文件、公告、承诺函、source.txt 好几个文件，
    但账本按项目编号登记，一个项目只有一条记录，所以任务粒度取项目。

    project_id 直接用目录名（如 20260828_27227490），天然唯一、可追溯。
    raw_dir 是这个项目的原始文件目录。
    """

    project_id: str
    raw_dir: Path


@dataclass(frozen=True)
class ParseOutcome:
    """一份文档跑完的结果。

    失败用状态字段表示，不抛异常。52 个项目里坏几个是常态（加密 PDF、损坏压缩包、
    扫描件糊到 OCR 认不出），一失败就抛，整批跑不完还得人守着。
    把失败装进结果继续跑，最后统一汇总。

    字段说明：
        status       parsed / skipped / failed
        parsed_path  产物文件名，相对 parsed_dir。跳过或失败时为 None
        quality_score 质量门 0~8 分。跳过时为 None，看账本里的历史值
        backend      mineru / mineru-vlm / plain-text
        needs_review 低分或者抽检命中，入库前需要人看
        reason       跳过原因、失败原因、页数说明，直接进日志
        elapsed_ms   本份耗时，批量跑时用来找异常慢的那几个
    """

    project_id: str
    status: str
    parsed_path: str | None = None
    quality_score: int | None = None
    backend: str | None = None
    needs_review: bool = False
    reason: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class ParseConfig:
    """一次批量执行要用的全部配置。

    所有阈值来自 rag.yaml，这里不设默认值。设了默认值就等于把阈值藏进代码：
    改 yaml 不生效，而且不会有任何提示，只能靠对数字才能发现。

    parsed_dir          产物目录
    probe_pages         探针抽几页（10 页够判断是不是扫描件）
    scanned_threshold   扫描件判定阈值，来自 router 段
    sample_rate         人工抽检率，来自 quality_gate 段
    quality             质量门的九个阈值
    source              主文件挑选策略
    mineru_timeout_s    单份解析超时秒数
    """

    parsed_dir: Path
    probe_pages: int
    scanned_threshold: float
    sample_rate: float
    quality: QualityThresholds
    source: SourcePolicy
    mineru_timeout_s: int

    @property
    def mineru_work_dir(self) -> Path:
        """MinerU 的中间产物目录：<parsed_dir 的上一级>/_mineru。

        不放进配置，因为它跟产物目录是绑定关系，单独配只会配错。
        放在产物同级而不是 inside，是为了让"重跑全部"能一条命令删干净：
        rm -rf data/parsed/_mineru，最终产物不受影响。
        """
        return self.parsed_dir.parent / "_mineru"

    @classmethod
    def from_mapping(cls, parsing: Mapping[str, Any], parsed_dir: Path) -> ParseConfig:
        """从 yaml 的 rag.offline.parsing 段构造。

        参数只收已经取出来的 parsing 段，不收整个文件：这个模块不该知道 yaml 长什么样，
        也不该 import yaml。读文件是脚本的事，这里只负责把 dict 变成有类型的对象。

        缺 key 会直接 KeyError。这是故意的——少配一项就当场炸在启动时，
        而不是跑完 52 份才发现其中几份用的是错阈值。
        """
        return cls(
            parsed_dir=parsed_dir,
            # 探针页数。int() 兜一层：yaml 里写成字符串也能转，写错则当场 ValueError。
            probe_pages=int(parsing["probe"]["pages"]),
            # 扫描件阈值。
            scanned_threshold=float(parsing["router"]["scanned_threshold"]),
            # 抽检率。
            sample_rate=float(parsing["quality_gate"]["sample_rate"]),
            # 质量门的九个阈值，交给它自己的工厂方法解析。
            quality=QualityThresholds.from_mapping(parsing["quality_gate"]),
            # 主文件挑选策略。
            source=SourcePolicy.from_mapping(parsing["source"]),
            # MinerU 单份超时。
            mineru_timeout_s=int(parsing["mineru"]["timeout_s"]),
        )
