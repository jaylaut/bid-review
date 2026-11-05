"""MinerU 解析接入。

这里不直接 import mineru 干活，而是起一个子进程调 `mineru_worker`。原因写在
`mineru_worker.py` 顶部：SDK 没有超时、内存峰值大、原生库崩了会连坐主进程。

为什么不用 MinerU 自带的 `mineru` 命令：4.x 的 CLI 换了架构，`mineru parse` 是客户端，
`basic`/`standard` 档要连本地 parse-server（不启就报 "Local parse-server is disabled"），
而且参数在 2.x、3.x、4.x 之间改过两轮。走 SDK 的 `mineru.parse()` 是官方 Python 入口，
不依赖服务端，也不用跟着 CLI 参数变。
"""
from __future__ import annotations

import locale
import os
import subprocess
import sys
from pathlib import Path

from common.errors import ConfigError, UpstreamError

__all__ = ["check_available", "parse_with_mineru", "tier_for_backend"]

# 项目的 src 目录：本文件在 src/rag/offline/parsers/ 下，往上第四级就是 src。
_SRC_DIR = Path(__file__).resolve().parents[3]

# 解析后端 → MinerU 4.x 的档位。
# mineru     ：文本型 PDF，走 onnx 小模型（版面 + OCR + 表格），4 GB 显存或纯 CPU 都能跑；
# mineru-vlm ：扫描件，要视觉模型，官方门槛是 8 GB 显存 + Turing 架构。
# 映射放在这里而不是配置文件：它跟本模块的实现绑定，换解析器时一起改才不容易漏。
_TIER_BY_BACKEND = {
    "mineru": "basic",
    "mineru-vlm": "advanced",
}


def check_available() -> None:
    """确认 mineru 已安装。

    单独抽出来，是为了让调用方能在跑批量之前先探一次，
    而不是跑到第 37 份才发现依赖没装。
    """
    try:
        # 只验证可导入，不在这个函数里调用解析。
        import mineru  # noqa: F401
    except ImportError as exc:
        raise ConfigError('没装 mineru。在 review-engine/ 下执行：uv add "mineru[core]"。') from exc


def tier_for_backend(backend: str) -> str:
    """把内部后端名换成 MinerU 档位。

    backend 是本项目自己的叫法（mineru / mineru-vlm），与厂商的 tier 名分开，
    换解析器时只改上面那张表。不认识的 backend 直接报错，不回落默认档——
    回落会让扫描件用文本档跑，出来一堆空页还不报错。
    """
    try:
        return _TIER_BY_BACKEND[backend]
    except KeyError as exc:
        raise ConfigError(f"未知解析后端：{backend}（可选 {list(_TIER_BY_BACKEND)}）") from exc


def _decode(raw: bytes | None) -> str:
    """把子进程输出解成文本。

    Windows 中文控制台默认 GBK，Linux 是 UTF-8，写死一种就会把中文日志变成问号。
    依次尝试，全失败用替换符，保证一定返回字符串。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_with_mineru(
    source: Path,
    *,
    backend: str,
    timeout_s: int,
    work_dir: Path,
) -> str:
    """解析一份文档，返回 markdown 文本。

    source    待解析文件（pdf / docx / pptx / xlsx / 图片）。
    backend   本项目的后端名，mineru 或 mineru-vlm。
    timeout_s 单份超时秒数，到点子进程会被 kill。
    work_dir  中间产物目录，每份文件产出同名 .md。

    失败抛 ConfigError（没装）或 UpstreamError（超时 / 崩了 / 空产物）。
    """
    # 先确认依赖在，避免起了子进程才发现缺包。
    check_available()

    # 档位映射：不认识的 backend 在进子进程之前就炸掉。
    tier = tier_for_backend(backend)

    # 产物路径固定为 <work_dir>/<源文件主干>.md，父子进程都知道它在哪，不用去猜。
    target = work_dir / f"{source.stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    # 子进程要能找到 rag 包。项目没把 src 装成包（pyproject 里 package = false），
    # 所以显式拼 PYTHONPATH，把 src 放最前面。
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_SRC_DIR}{os.pathsep}{existing}" if existing else str(_SRC_DIR)

    # 用当前解释器起子进程，保证父子同一个 venv。
    command = [
        sys.executable,
        "-m",
        "rag.offline.parsers.mineru_worker",
        str(source),
        tier,
        str(target),
    ]

    try:
        result = subprocess.run(  # noqa: S603  参数是列表，没有 shell 注入面
            command,
            capture_output=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # 超时通常是文件本身有问题（损坏、页数过多、渲染卡死），重试大概率还是超时。
        raise UpstreamError(
            f"解析超时（{timeout_s}s）：{source.name}",
            details={"source": str(source), "tier": tier},
            retriable=False,
        ) from exc

    # 非 0 返回码：把 stderr 尾部带出来。只报"失败"没有意义，得知道崩在哪一步。
    if result.returncode != 0:
        raise UpstreamError(
            f"MinerU 解析失败（退出码 {result.returncode}）：{source.name}",
            details={
                "source": str(source),
                "tier": tier,
                "stderr_tail": _decode(result.stderr)[-800:],
            },
            retriable=False,
        )

    # 子进程说成功但文件不存在，说明协议被破坏，同样按失败处理。
    if not target.is_file():
        raise UpstreamError(
            f"解析进程没有写出产物：{source.name}",
            details={"expected": str(target)},
            retriable=False,
        )

    # 读回 markdown。坏字节忽略：一个坏字符不该让整份文档读不出来。
    markdown = target.read_text(encoding="utf-8", errors="ignore")

    # 空产物等于没解析成功（整份都是图、或解析器提前退出），不能当正常结果放过去。
    if not markdown.strip():
        raise UpstreamError(
            f"解析产物为空：{source.name}",
            details={"source": str(source), "tier": tier},
            retriable=False,
        )

    return markdown
