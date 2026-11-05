"""parsers 包的对外入口。

这里放两样东西：纯文本通道和标题层级规整。两者都不依赖 MinerU，是纯函数。
路由在 route.py，MinerU 调用在 mineru.py，这里只做转出，方便调用方一处 import。

包内不要再各自实现一份路由或解析逻辑——同一套规则两个副本，改一处另一处必然漏改。
"""
from __future__ import annotations

import re
from pathlib import Path

from common.errors import ConfigError
from rag.offline.parsers.mineru import build_command, find_cli, locate_markdown, parse_with_mineru
from rag.offline.parsers.route import ParseRoute, detect_route, sample_pages

# 显式声明对外接口。调用方和 IDE 都靠它知道这个包提供什么。
__all__ = [
    "ParseRoute",
    "build_command",
    "detect_route",
    "find_cli",
    "locate_markdown",
    "normalize_markdown",
    "parse_plain_text",
    "parse_with_mineru",
    "sample_pages",
]

# 标题行：行首 1~6 个 #，后面必须跟空白。
# MinerU 导出的标题都带空格；不带空格的 "#标题" 在 markdown 里本来也不算标题。
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# 代码块围栏。围栏内的 "# xxx" 是注释，不是标题，不能被改写。
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# 能直读的后缀。
_PLAIN_SUFFIXES = {".txt", ".md"}


def parse_plain_text(path: str | Path) -> str:
    """纯文本通道：.txt / .md 直接读成字符串。

    这条通道不是玩具。调清洗、分块、嵌入、检索的时候不用等 MinerU，
    拿一份 txt 就能把后面的链路跑通；解析器的 bug 和下游的 bug 也能分开定位。

    path 可以是字符串或 Path。格式不支持时抛 ConfigError，并给出两条可行路径。
    """
    # 统一成 Path，这样传字符串和传 Path 都能用，不用调用方先转一道。
    file_path = Path(path)

    # 后缀小写化，避免 .MD 这种写法被漏掉。
    suffix = file_path.suffix.lower()

    # 两种纯文本格式直读。
    if suffix in _PLAIN_SUFFIXES:
        # errors="ignore"：坏字节不该让整份文档读不出来。
        return file_path.read_text(encoding="utf-8", errors="ignore")

    # 其它格式：报错要说清下一步怎么做，不能只说"不支持"。
    raise ConfigError(
        f"不能直接读 {suffix or '无后缀文件'}：装 MinerU（uv add mineru）后走 parse_with_mineru，"
        "或者先把文档转成 .md 用纯文本通道。"
    )


def normalize_markdown(md: str) -> str:
    """纠正标题跳级：把"# 下面直接跟 ###"压回 "##"。

    真实标书导出的 markdown 经常跳级。不纠正的话，分块按级别累加编号会切出幽灵层级，
    section_path 出现 "3>1" 这种断档，引用定位（"依据 3.2.4 节 P87"）全错，而且不报错。

    代码块内部不动。围栏里的 "# 安装依赖" 是 shell 注释，按标题改写会把命令弄坏。

    md 是原始 markdown，返回层级连续的 markdown。
    """
    # 逐行处理再拼回去。按行而不是按字符，是因为标题和围栏都是行级结构。
    lines = md.splitlines()

    # 结果行。
    out: list[str] = []

    # 上一行标题的级别。0 表示还没遇到过标题。
    prev_level = 0

    # 当前是否在代码块里。围栏成对出现，进来一次出去一次。
    in_fence = False

    # 逐行扫。
    for line in lines:
        # 围栏行本身原样保留，并切换状态。
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue

        # 围栏内的内容一律不动：里面的 # 是注释、是配置、是命令行提示符。
        if in_fence:
            out.append(line)
            continue

        # 不是标题行就原样保留。match 只从行首匹配，正文里的 # 不会命中。
        match = _HEAD_RE.match(line)
        if not match:
            out.append(line)
            continue

        # 当前标题级别，等于 # 的个数。
        level = len(match.group(1))

        # 跳级判定：比上一级深两档以上就压回"上一级 +1"。
        # prev_level 为 0 时不压——文档第一个标题可以是任意级别。
        if prev_level and level > prev_level + 1:
            level = prev_level + 1

        # 按新级别重写这一行，标题文字不动。
        out.append("#" * level + " " + match.group(2))

        # 记住这一级，供下一行比较。
        prev_level = level

    # 拼回整份文本。原文件末尾有没有换行，这里不补也不删。
    return "\n".join(out)
