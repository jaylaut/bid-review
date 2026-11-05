"""MinerU 调用。走命令行，不碰它的内部 Python 类。

为什么用 CLI 而不是 import：magic-pdf 1.x 是 Pipeline(doc_bytes=...)，2.x 换成
PymuDocDataset + doc_analyze，内部类名中途改过。命令行入口一直是 `-p 输入 -o 输出`，
两个版本都保留，是最稳的调用方式。

代价是要自己处理两件事：产物到底落在哪个文件、以及卡住不返回怎么办。都在下面处理。
"""
from __future__ import annotations

import locale
import shutil
import subprocess
import sys
from pathlib import Path

from common.errors import ConfigError, UpstreamError

__all__ = ["build_command", "find_cli", "locate_markdown", "parse_with_mineru"]

# 2.x 的入口名。包名也从 magic-pdf 改成了 mineru。
_CLI_V2 = "mineru"
# 1.x 的入口名。
_CLI_V1 = "magic-pdf"

# MinerU 2.x 移除了内置的 LibreOffice 转换，Office 格式需要先转 PDF。
# 这里不强行拦截（不同版本行为有差异），只在报错时给提示。
_OFFICE_SUFFIXES = {".doc", ".docx"}


def _venv_script(name: str) -> list[Path]:
    """列出当前解释器所在目录下的可执行文件候选路径。

    为什么要这一步：用 `uv run` 跑时，虚拟环境的 Scripts 目录会被加进 PATH，
    shutil.which 能找到 mineru；但直接用 `.venv/Scripts/python.exe` 跑，
    PATH 里没有这个目录，which 就找不到已经装好的命令。
    补一次"跟当前解释器同目录"的探测，两种情况都能用。

    name 是不带后缀的命令名。
    """
    # 当前解释器所在目录：虚拟环境里就是 Scripts（Windows）或 bin（Linux/macOS）。
    bin_dir = Path(sys.executable).parent
    # Windows 上 pip 装出来的命令带 .exe，Unix 上不带。
    if sys.platform == "win32":
        return [bin_dir / f"{name}.exe", bin_dir / name]
    return [bin_dir / name]


def find_cli() -> tuple[str, int]:
    """找可用的 MinerU 命令。

    返回 (可执行文件路径, 大版本号)。版本号只用来决定参数怎么写。
    找不到就抛 ConfigError 并给出安装命令，不要丢一个裸 FileNotFoundError 出去。
    """
    # 先找 2.x：PATH 里找，再找解释器同目录。
    for candidate in [shutil.which(_CLI_V2), *[str(p) for p in _venv_script(_CLI_V2)]]:
        if candidate and Path(candidate).is_file():
            return candidate, 2

    # 再找 1.x，顺序同上。
    for candidate in [shutil.which(_CLI_V1), *[str(p) for p in _venv_script(_CLI_V1)]]:
        if candidate and Path(candidate).is_file():
            return candidate, 1

    # 一个都没有。报错里给出能直接照抄的命令，而不是只说"找不到"。
    raise ConfigError(
        "没找到 MinerU 命令行。在 review-engine/ 下执行：uv add mineru（2.x）"
        "或 uv add magic-pdf==1.3.12（1.x）。\n"
        "只想先调下游（清洗/分块/检索）的话，把 .txt 放进项目目录走纯文本通道即可。"
    )


def build_command(exe: str, major: int, source: Path, out_dir: Path, backend: str) -> list[str]:
    """拼命令行参数。

    exe 是可执行文件路径，major 是大版本号，source 是待解析文件，
    out_dir 是输出目录，backend 是 mineru / mineru-vlm。

    返回列表而不是字符串：不经过 shell，路径里的空格、中文、括号都不用转义。
    """
    # 2.x：-b 选后端。pipeline 是常规版面分析，vlm 是视觉模型，能处理扫描件。
    if major >= 2:
        mode = "vlm" if backend == "mineru-vlm" else "pipeline"
        return [exe, "-p", str(source), "-o", str(out_dir), "-b", mode]

    # 1.x：-m 选方法。auto 让 MinerU 自己判，ocr 强制走 OCR。
    mode = "ocr" if backend == "mineru-vlm" else "auto"
    return [exe, "-p", str(source), "-o", str(out_dir), "-m", mode]


def _decode(raw: bytes | None) -> str:
    """把子进程输出解成文本。

    Windows 中文系统的命令行默认输出 GBK，Linux/macOS 是 UTF-8。
    写死 utf-8 会把中文日志解成一堆问号，排查时等于没有日志。
    依次试几种编码，全失败就用替换符，保证一定有字符串返回。
    """
    # 没有输出直接返回空串。
    if not raw:
        return ""
    # 逐个尝试：先 utf-8（MinerU 自己写日志多用这个），再系统默认，最后 gbk。
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # 都失败：用替换符解，至少保留能看懂的部分。
    return raw.decode("utf-8", errors="replace")


def locate_markdown(out_dir: Path, source_stem: str) -> Path:
    """在输出目录里找 MinerU 产出的正文 markdown。

    MinerU 的输出层级是 <out>/<文件名主干>/<后端>/<文件名主干>.md，但后端目录名
    （auto / pipeline / vlm / hybrid）和层级在版本之间变过，硬编码路径迟早失效。

    所以先按文件名找：正文一定和源文件同名。名字对不上（MinerU 改过命名规则）
    再退回"体积最大的 .md"——图片是 .jpg、中间结果是 .json，正文必然是最大的那个 .md。

    out_dir 是输出根目录，source_stem 是源文件名去掉后缀。
    """
    # 递归收集所有 md。rglob 返回生成器，这时还没真正遍历目录。
    md_paths = out_dir.rglob("*.md")

    # 过滤出真正的文件。rglob 匹配的是路径，同名的目录也会被匹配到。
    candidates: list[Path] = []
    for path in md_paths:
        if path.is_file():
            candidates.append(path)

    # 一个都没有：CLI 参数不对、文件加密、或模型没下载，都可能是原因。
    if not candidates:
        raise UpstreamError(
            f"MinerU 没产出 markdown：{out_dir}",
            details={"out_dir": str(out_dir)},
            retriable=False,
        )

    # 第一优先：文件名正好等于源文件主干。这条不依赖目录层级，最可靠。
    for path in candidates:
        if path.stem == source_stem:
            return path

    # 第二优先：体积最大的那个。排完序取第一个。
    candidates.sort(key=lambda item: item.stat().st_size, reverse=True)
    return candidates[0]


def parse_with_mineru(
    source: Path,
    *,
    work_dir: Path,
    backend: str,
    timeout_s: int,
) -> str:
    """用 MinerU 解析一份文件，返回 markdown 文本。

    source 是待解析文件，work_dir 是输出根目录（每份文件在其下占一个子目录），
    backend 是 mineru / mineru-vlm，timeout_s 是单份超时秒数。

    异常：ConfigError（没装）、UpstreamError（超时 / 没产物 / 返回码非 0）。
    """
    # 找命令行，同时拿到大版本号，后面拼参数要用。
    exe, major = find_cli()

    # 每份文件单独一个输出目录，避免同名文件（不同项目里都叫"招标文件.pdf"）互相覆盖。
    out_dir = work_dir / source.stem

    # 先建目录。MinerU 不会自己建父目录，传一个不存在的路径会直接报错。
    out_dir.mkdir(parents=True, exist_ok=True)

    # 拼参数。
    command = build_command(exe, major, source, out_dir, backend)

    # 调子进程。capture_output 把 stdout/stderr 收进内存，
    # 不打印到控制台——批量跑 52 份时，MinerU 的输出会把终端刷爆。
    try:
        result = subprocess.run(  # noqa: S603  参数是列表、exe 来自 which，没有 shell 注入面
            command,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # 超时单独报。同一个文件重试大概率还是超时，所以标成不可重试。
        raise UpstreamError(
            f"MinerU 解析超时（{timeout_s}s）：{source.name}",
            details={"source": str(source)},
            retriable=False,
        ) from exc

    # 返回码非 0：把 stderr 最后几百个字符带进异常。
    # 不带的话，日志里只剩"返回码 1"，什么信息都没有。
    if result.returncode != 0:
        tail = _decode(result.stderr)[-800:]
        hint = ""
        # Office 格式在 2.x 上经常直接失败（内置 LibreOffice 被移除了），
        # 单独补一句提示，省得去猜是文件坏了还是环境问题。
        if major >= 2 and source.suffix.lower() in _OFFICE_SUFFIXES:
            hint = "\n提示：MinerU 2.x 不支持直接解析 Office 文档，先用 LibreOffice 转成 PDF。"
        raise UpstreamError(
            f"MinerU 返回码 {result.returncode}：{source.name}{hint}",
            details={"source": str(source), "stderr_tail": tail},
            retriable=False,
        )

    # 找产物。
    markdown_path = locate_markdown(out_dir, source.stem)

    # 读出来。errors="ignore"：MinerU 偶尔会在正文里留非法字节，不能因此整份读不出。
    return markdown_path.read_text(encoding="utf-8", errors="ignore")
