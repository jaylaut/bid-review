# -*- coding: utf-8 -*-
"""解析流水线：一个项目目录进，一份干净 markdown + 一条账本记录出。

这里只有顺序和取舍，没有算法。切页、清洗、打分、写账本都在各自模块里。

两个顺序约束不能改：
- 先落盘再登记。中间崩一次，账本会声称"解析好了"而产物不存在，下次断点续跑直接跳过，
  数据永久缺失。先落盘最坏是重跑一遍，浪费点 CPU。
- 先 normalize 再 clean。清洗会整行删（页眉页脚、水印），先纠正标题层级，
  删完剩下的层级才连续，R2 按级累加编号才不会出现 3>1 这种断档。

断点续跑靠账本里三个字段：项目编号、原件哈希、产物文件名。
"""
from __future__ import annotations

import re               # 只用于把项目编号里的非法字符换成下划线
import time
from collections.abc import Callable,Sequence       # 标注on_outcome 回调和tasks列表的类型，用于静态检查
from datetime import datetime,timezone              # 账本时间戳，带时区
from pathlib import Path

from common.fsutil import write_text_atomic         # 原子写：先临时文件在替换
from common.ids import sha256_file                  # 文件哈希，本项目唯一实现
from rag.offline.cleaning import clean_document

# 三种状态常量 + 三个数据契约。常量是为了避免 "skip"/"skipped" 这种拼错。
from rag.offline.parsers.contracts import (
    FAILED,          # "failed"：本次没成功，下次会重试（失败不登记账本）
    PARSED,          # "parsed"：本次解析成功并已登记
    SKIPPED,         # "skipped"：断点续跑命中，没碰解析器
    ParseConfig,     # 阈值与路径的聚合对象，全部来自 rag.yaml
    ParseOutcome,    # 一份文档的结果
    ParseTask,       # 一个项目任务
)
# Manifest：账本读写（自带文件锁、合并式落盘）。ManifestEntry：账本里的一行。
from rag.offline.manifest import Manifest, ManifestEntry
# detect_route 判扫描件/文本型；normalize_markdown 纠标题跳级；
# parse_plain_text 是 .txt/.md 直读通道；parse_with_mineru 是统一解析入口。
from rag.offline.parsers import (
    detect_route,
    normalize_markdown,
    parse_plain_text,
    parse_with_mineru,
)
# 质量门：0~8 打分，阈值对象由 cfg 传进来，模块内没有默认值。
from rag.offline.quality_gate import score_parse_quality
# 主文件挑选：纯策略、无 IO 写，放在 source.py 便于单测。
from rag.offline.source import select_main_file